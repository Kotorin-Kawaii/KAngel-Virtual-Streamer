"""
直播间元信息推送服务
管理直播间实时元信息，包括在线人数、用户进出、时间日期等
具有良好的扩展性，支持后期添加新的元信息字段
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Set, Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum
from fastapi import WebSocket

from config import settings
from kangel.shared.logging import logger
from kangel.transport.websocket.protocol import WebSocketEventType
from ..domain.schedule import StreamScheduleService
from .daily_theme import DailyThemeService
from .activity import StreamerActivityService, StreamerActivityState
from .beat import StreamerBeatScheduler
from .idle_state import IdleState, IdleStateResolver
from .mainline import DailyStreamPlanService, StreamMainlineService
from ..domain.mainline import StreamMainlineState
from .director import (
    ActionExecutionResult,
    DirectorPerformanceTemplates,
    PerformanceAction,
    StreamDirectorRuntime,
    StreamerActionDecision,
    StreamerActionExecutor,
)


class MetadataEventType(Enum):
    """元信息事件类型"""
    USER_JOIN = "user_join"          # 用户加入
    USER_LEAVE = "user_leave"        # 用户离开
    VIEWER_COUNT = "viewer_count"    # 在线人数更新
    TIME_SYNC = "time_sync"          # 时间同步
    STREAM_STATUS = "stream_status"  # 直播状态
    CUSTOM = "custom"                # 自定义事件


@dataclass
class StreamMetadata:
    """直播间元信息数据类"""
    # 基础信息
    stream_id: str = "default"
    streamer_name: str = settings.persona.streamer_name
    
    # 观众信息
    viewer_count: int = 0
    total_joined: int = 0
    total_left: int = 0
    
    # 时间信息
    current_time: str = field(default_factory=lambda: datetime.now().isoformat())
    stream_start_time: Optional[str] = None
    stream_duration_seconds: int = 0
    
    # 直播状态
    is_live: bool = False
    stream_status: str = "offline"  # streaming, offline
    schedule_timezone: str = "UTC"
    schedule_config_valid: bool = True
    schedule_errors: List[str] = field(default_factory=list)
    current_stream_start_time: Optional[str] = None
    current_stream_end_time: Optional[str] = None
    next_stream_start_time: Optional[str] = None
    next_stream_end_time: Optional[str] = None
    daily_theme_id: str = "just-chatting"
    daily_theme_name: str = "轻松杂谈"
    daily_theme_date: str = ""
    theme_config_valid: bool = True
    theme_errors: List[str] = field(default_factory=list)
    special_date_theme: Optional[Dict[str, Any]] = None
    stream_session_id: Optional[str] = None
    session_theme: Optional[Dict[str, Any]] = None
    daily_stream_plan: Optional[Dict[str, Any]] = None
    current_mainline_beat: Optional[Dict[str, Any]] = None
    mainline_config_valid: bool = True
    mainline_errors: List[str] = field(default_factory=list)
    current_activity: Optional[Dict[str, Any]] = None
    activity_config_valid: bool = True
    activity_errors: List[str] = field(default_factory=list)
    streamer_idle_state: Optional[Dict[str, Any]] = None
    
    # 扩展字段（用于后期添加新字段）
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        data = asdict(self)
        # 更新时间
        data['current_time'] = datetime.now(timezone.utc).isoformat()
        # 计算直播时长
        if self.stream_start_time:
            start = datetime.fromisoformat(self.stream_start_time)
            now = datetime.now(start.tzinfo or timezone.utc)
            duration = now - start
            data['stream_duration_seconds'] = int(duration.total_seconds())
        return data


@dataclass
class UserActivity:
    """用户活动记录"""
    user_id: str
    nickname: str
    action: str  # join, leave
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return asdict(self)


class StreamMetadataPusher:
    """直播间元信息推送器"""
    
    def __init__(
        self,
        schedule_service: Optional[StreamScheduleService] = None,
        theme_service: Optional[DailyThemeService] = None,
        activity_service: Optional[StreamerActivityService] = None,
        beat_scheduler: Optional[StreamerBeatScheduler] = None,
        mainline_service: Optional[StreamMainlineService] = None,
        director_runtime: Optional[StreamDirectorRuntime] = None,
    ):
        self._subscribers: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._running = False
        self._push_task: Optional[asyncio.Task] = None
        
        # 元信息数据
        self._metadata = StreamMetadata()
        self._schedule_from_settings = schedule_service is None
        self._schedule = schedule_service or StreamScheduleService(
            settings.stream.timezone, settings.stream.weekly_schedule
        )
        self._theme_from_settings = theme_service is None
        schedule_zone = getattr(
            self._schedule, "zone",
            getattr(getattr(self._schedule, "service", None), "zone", timezone.utc),
        )
        self._theme = theme_service or DailyThemeService(
            schedule_zone, settings.stream.daily_themes,
            settings.stream.special_date_themes,
        )
        self._activity_service = activity_service
        self._activity_from_settings = activity_service is None
        self._beat_scheduler = beat_scheduler
        self._mainline_service = mainline_service
        self._mainline_from_settings = mainline_service is None
        self._current_mainline: Optional[StreamMainlineState] = None
        self._director_runtime = director_runtime
        self._director_templates = DirectorPerformanceTemplates()
        self._last_activity_proposal: Optional[dict] = None
        self._intent_service = None
        self._session_summary_service = None
        self._current_activity: Optional[StreamerActivityState] = None
        self._last_activity_evaluation_at = 0.0
        self._last_activity_event_version: dict[str, int] = {}
        self._last_activity_transition_at = 0.0
        self._idle_state_resolver = IdleStateResolver()
        self._idle_state: Optional[IdleState] = None
        self._idle_state_version = 0
        self._idle_state_changed_at = 0.0
        self._idle_fact_signature: Optional[tuple] = None
        self._last_theme_metric_signature: Optional[tuple] = None
        self._user_activities: List[UserActivity] = []
        self._max_activities = 100  # 最大保留的活动记录数
        
        # 推送配置
        self._push_interval_ms = 5000  # 默认5秒推送一次
        self._enable_push = True
        
        # 统计
        self._stats = {
            "total_pushes": 0,
            "start_time": None,
            "last_push_time": None,
            "activity_initializations": 0,
            "activity_silent_switches": 0,
            "activity_public_switches": 0,
            "activity_suggestions_accepted": 0,
            "activity_suggestions_suppressed": 0,
            "special_date_theme_hits": 0,
            "theme_config_degraded": 0,
            "idle_state_changes": 0,
            "idle_reason_offline": 0,
            "idle_reason_special_date": 0,
            "idle_reason_current_activity": 0,
            "idle_reason_persona": 0,
            "idle_reason_default": 0,
            "streamer_beat_emitted": 0,
        }
        
        # 扩展处理器（用于后期添加新的元信息处理逻辑）
        self._extension_handlers: Dict[str, Callable] = {}
    
    async def start(self):
        """启动推送服务"""
        if not self._enable_push:
            logger.info("直播间元信息推送服务已禁用")
            return
        
        if self._running:
            logger.warning("直播间元信息推送服务已在运行")
            return

        if self._schedule_from_settings:
            # lifespan 中 config.json 覆盖发生在全局实例构造之后，因此启动时重读。
            self._schedule = StreamScheduleService(
                settings.stream.timezone, settings.stream.weekly_schedule
            )
        if self._theme_from_settings:
            self._theme = DailyThemeService(
                self._schedule.zone, settings.stream.daily_themes,
                settings.stream.special_date_themes,
            )
        self._ensure_activity_service()
        self._ensure_beat_scheduler()
        if self._mainline_from_settings:
            self._mainline_service = None
        self._ensure_mainline_service()
        
        self._running = True
        self._refresh_schedule()
        if settings.stream.director_enabled:
            runtime = self._ensure_director_runtime()
            from kangel.infrastructure.event_bus import persona_event_pipeline
            persona_event_pipeline.add_observer(runtime.observe_persona_event)
            await runtime.start()
        self._stats["start_time"] = datetime.now().isoformat()
        self._push_task = asyncio.create_task(self._push_loop())
        
        logger.info(f"直播间元信息推送服务启动，推送间隔: {self._push_interval_ms}ms")
    
    async def stop(self):
        """停止推送服务"""
        if not self._running:
            return
        
        self._running = False
        
        if self._push_task:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        if self._director_runtime:
            from kangel.infrastructure.event_bus import persona_event_pipeline
            persona_event_pipeline.remove_observer(
                self._director_runtime.observe_persona_event
            )
            await self._director_runtime.stop()
        
        logger.info("直播间元信息推送服务已停止")
    
    async def subscribe(self, websocket: WebSocket):
        """订阅元信息推送"""
        async with self._lock:
            self._subscribers.add(websocket)
        
        # 立即发送一次当前状态
        await self._send_metadata_to_client(websocket)
        
        logger.debug(f"新客户端订阅元信息推送，当前订阅数: {len(self._subscribers)}")
    
    async def unsubscribe(self, websocket: WebSocket):
        """取消订阅"""
        async with self._lock:
            if websocket in self._subscribers:
                self._subscribers.discard(websocket)
                logger.debug(f"客户端取消订阅元信息推送，当前订阅数: {len(self._subscribers)}")
    
    def update_viewer_count(self, count: int):
        """更新在线人数"""
        old_count = self._metadata.viewer_count
        self._metadata.viewer_count = count
        
        # 如果人数变化较大，立即推送
        if abs(count - old_count) >= 5:
            asyncio.create_task(self._broadcast_viewer_count())

    async def notify_director_event(self, family: str, *, priority: int = 5) -> bool:
        """供 SC 等现有子系统发送可合并信号；Director 关闭时是无副作用空操作。"""
        if not settings.stream.director_enabled or not self._director_runtime:
            return False
        try:
            if family == "sc_completed":
                self._director_runtime.signals.record_activity()
            return await self._director_runtime.notify(family, priority=priority)
        except Exception as exc:
            logger.debug("Director 外部信号已忽略: family=%s error=%s", family, exc)
            return False
    
    def record_user_join(self, user_id: str, nickname: str, **extra):
        """记录用户加入"""
        self._metadata.total_joined += 1
        
        activity = UserActivity(
            user_id=user_id,
            nickname=nickname,
            action="join",
            extra=extra
        )
        self._add_activity(activity)
        
        # 立即推送用户加入事件
        asyncio.create_task(self._broadcast_user_activity(activity))
        
        logger.debug(f"用户加入: {nickname} (ID: {user_id})")
    
    def record_user_leave(self, user_id: str, nickname: str, **extra):
        """记录用户离开"""
        self._metadata.total_left += 1
        
        activity = UserActivity(
            user_id=user_id,
            nickname=nickname,
            action="leave",
            extra=extra
        )
        self._add_activity(activity)
        
        # 立即推送用户离开事件
        asyncio.create_task(self._broadcast_user_activity(activity))
        
        logger.debug(f"用户离开: {nickname} (ID: {user_id})")
    
    def _add_activity(self, activity: UserActivity):
        """添加活动记录"""
        self._user_activities.append(activity)
        # 限制历史记录数量
        if len(self._user_activities) > self._max_activities:
            self._user_activities.pop(0)
    
    def update_stream_status(self, status: str):
        """兼容旧调用；排期开关为唯一真相源，拒绝手动覆盖。"""
        logger.warning("忽略手动直播状态 %r；当前状态由直播排期决定", status)
        self._refresh_schedule()
        asyncio.create_task(self._broadcast_stream_status())
    
    def set_extra_field(self, key: str, value: Any):
        """设置扩展字段"""
        self._metadata.extra[key] = value
    
    def get_extra_field(self, key: str, default=None) -> Any:
        """获取扩展字段"""
        return self._metadata.extra.get(key, default)
    
    def register_extension_handler(self, event_type: str, handler: Callable):
        """注册扩展处理器"""
        self._extension_handlers[event_type] = handler
        logger.info(f"注册扩展处理器: {event_type}")
    
    async def _push_loop(self):
        """推送循环"""
        interval_seconds = self._push_interval_ms / 1000.0
        
        while self._running:
            try:
                boundary = self._schedule.seconds_until_change()
                theme_boundary = self._theme.seconds_until_change()
                wait_seconds = interval_seconds
                if boundary is not None:
                    wait_seconds = max(0.05, min(interval_seconds, boundary + 0.05))
                wait_seconds = max(
                    0.05, min(wait_seconds, theme_boundary + 0.05)
                )
                await asyncio.sleep(wait_seconds)
                changed = self._refresh_schedule()
                await self._maybe_emit_streamer_beat()
                
                if self._subscribers:
                    await self._broadcast_metadata()
                    if changed:
                        await self._broadcast_stream_status()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"元信息推送循环出错: {e}")
                await asyncio.sleep(1)
    
    async def _broadcast_metadata(self):
        """广播完整元信息"""
        self._refresh_schedule()
        metadata_dict = self._metadata.to_dict()
        
        message = {
            "type": WebSocketEventType.STREAM_METADATA,
            "data": metadata_dict,
            "timestamp": datetime.now().isoformat()
        }
        
        await self._broadcast_to_all(message)
        
        self._stats["total_pushes"] += 1
        self._stats["last_push_time"] = datetime.now().isoformat()
        
        if self._stats["total_pushes"] % 12 == 0:  # 每分钟记录一次
            logger.debug(f"元信息推送 #{self._stats['total_pushes']}，订阅数: {len(self._subscribers)}")
    
    async def _broadcast_viewer_count(self):
        """广播在线人数更新"""
        message = {
            "type": WebSocketEventType.VIEWER_COUNT_UPDATE,
            "data": {
                "viewer_count": self._metadata.viewer_count,
                "total_joined": self._metadata.total_joined,
                "total_left": self._metadata.total_left
            },
            "timestamp": datetime.now().isoformat()
        }
        
        await self._broadcast_to_all(message)
        logger.debug(f"在线人数更新: {self._metadata.viewer_count}")
    
    async def _broadcast_user_activity(self, activity: UserActivity):
        """广播用户活动"""
        message = {
            "type": WebSocketEventType.USER_ACTIVITY,
            "data": activity.to_dict(),
            "timestamp": datetime.now().isoformat()
        }
        
        await self._broadcast_to_all(message)
    
    async def _broadcast_stream_status(self):
        """广播直播状态"""
        message = {
            "type": WebSocketEventType.STREAM_STATUS,
            "data": {
                "is_live": self._metadata.is_live,
                "stream_status": self._metadata.stream_status,
                "stream_duration_seconds": self._metadata.to_dict()["stream_duration_seconds"],
                "schedule_timezone": self._metadata.schedule_timezone,
                "schedule_config_valid": self._metadata.schedule_config_valid,
                "schedule_errors": self._metadata.schedule_errors,
                "current_stream_start_time": self._metadata.current_stream_start_time,
                "current_stream_end_time": self._metadata.current_stream_end_time,
                "next_stream_start_time": self._metadata.next_stream_start_time,
                "next_stream_end_time": self._metadata.next_stream_end_time,
                "daily_theme_id": self._metadata.daily_theme_id,
                "daily_theme_name": self._metadata.daily_theme_name,
                "daily_theme_date": self._metadata.daily_theme_date,
                "theme_config_valid": self._metadata.theme_config_valid,
                "theme_errors": self._metadata.theme_errors,
                "special_date_theme": self._metadata.special_date_theme,
                "stream_session_id": self._metadata.stream_session_id,
                "session_theme": self._metadata.session_theme,
                "daily_stream_plan": self._metadata.daily_stream_plan,
                "current_mainline_beat": self._metadata.current_mainline_beat,
                "mainline_config_valid": self._metadata.mainline_config_valid,
                "mainline_errors": self._metadata.mainline_errors,
                "current_activity": self._metadata.current_activity,
                "activity_config_valid": self._metadata.activity_config_valid,
                "activity_errors": self._metadata.activity_errors,
                "streamer_idle_state": self._metadata.streamer_idle_state,
            },
            "timestamp": datetime.now().isoformat()
        }
        
        await self._broadcast_to_all(message)
    
    async def _broadcast_to_all(self, message: dict):
        """广播给所有订阅者"""
        disconnected = set()
        message_str = json.dumps(message, ensure_ascii=False)
        
        async with self._lock:
            for websocket in self._subscribers:
                try:
                    await websocket.send_text(message_str)
                except Exception as e:
                    logger.error(f"发送元信息失败: {e}")
                    disconnected.add(websocket)
            
            # 移除断开的连接
            for ws in disconnected:
                self._subscribers.discard(ws)
    
    async def _send_metadata_to_client(self, websocket: WebSocket):
        """发送元信息给单个客户端"""
        try:
            self._refresh_schedule()
            metadata_dict = self._metadata.to_dict()
            message = {
                "type": WebSocketEventType.STREAM_METADATA,
                "data": metadata_dict,
                "timestamp": datetime.now().isoformat()
            }
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
        except Exception as e:
            logger.error(f"发送元信息给新客户端失败: {e}")
    
    def get_metadata(self) -> StreamMetadata:
        """获取当前元信息"""
        self._refresh_schedule()
        return self._metadata

    def _refresh_schedule(self) -> bool:
        snapshot = self._schedule.evaluate()
        previous = (
            self._metadata.is_live,
            self._metadata.current_stream_start_time,
            self._metadata.current_stream_end_time,
            self._metadata.next_stream_start_time,
            self._metadata.daily_theme_id,
            self._metadata.daily_theme_date,
            (self._metadata.special_date_theme or {}).get("id"),
            (self._metadata.current_activity or {}).get("version"),
            (self._metadata.current_mainline_beat or {}).get("version"),
        )
        self._metadata.is_live = snapshot.is_live
        self._metadata.stream_status = snapshot.stream_status
        self._metadata.schedule_timezone = snapshot.schedule_timezone
        self._metadata.schedule_config_valid = snapshot.schedule_config_valid
        self._metadata.schedule_errors = snapshot.schedule_errors
        self._metadata.current_stream_start_time = snapshot.current_stream_start_time
        self._metadata.current_stream_end_time = snapshot.current_stream_end_time
        self._metadata.next_stream_start_time = snapshot.next_stream_start_time
        self._metadata.next_stream_end_time = snapshot.next_stream_end_time
        theme_reference = (
            datetime.fromisoformat(snapshot.current_stream_start_time)
            if snapshot.is_live and snapshot.current_stream_start_time else None
        )
        # 一场跨午夜直播继续使用开播日期的主题；下播后才回到自然日主题。
        try:
            theme = self._theme.evaluate(theme_reference)
        except TypeError:
            # 保留测试/插件中旧的无参数 Theme provider 契约。
            theme = self._theme.evaluate()
        self._metadata.daily_theme_id = theme.daily_theme_id
        self._metadata.daily_theme_name = theme.daily_theme_name
        self._metadata.daily_theme_date = theme.daily_theme_date
        self._metadata.theme_config_valid = theme.theme_config_valid
        self._metadata.theme_errors = theme.theme_errors
        self._metadata.special_date_theme = theme.special_date_theme
        self._record_theme_metrics(theme)
        self._refresh_activity(snapshot, theme)
        self._refresh_mainline(snapshot, theme)
        self._refresh_session_summary(snapshot, theme)
        self._refresh_idle_state(snapshot, theme)
        # 兼容旧字段：仅在开播时给出本场排期起点。
        self._metadata.stream_start_time = snapshot.current_stream_start_time
        if not snapshot.is_live:
            self._metadata.stream_duration_seconds = 0
        current = (
            self._metadata.is_live,
            self._metadata.current_stream_start_time,
            self._metadata.current_stream_end_time,
            self._metadata.next_stream_start_time,
            self._metadata.daily_theme_id,
            self._metadata.daily_theme_date,
            (self._metadata.special_date_theme or {}).get("id"),
            (self._metadata.current_activity or {}).get("version"),
            (self._metadata.current_mainline_beat or {}).get("version"),
        )
        changed = previous != current
        if changed:
            logger.info(
                "直播排期状态更新: is_live=%s current=%s..%s next=%s",
                snapshot.is_live,
                snapshot.current_stream_start_time,
                snapshot.current_stream_end_time,
                snapshot.next_stream_start_time,
            )
            if (
                self._director_runtime
                and (previous[0] != current[0] or previous[1] != current[1])
            ):
                try:
                    asyncio.get_running_loop().create_task(
                        self._director_runtime.notify("stream_lifecycle", priority=1)
                    )
                except RuntimeError:
                    pass
        return changed

    def _record_theme_metrics(self, theme) -> None:
        """主题指标只按自然日/配置状态变化累计，避免每次元数据刷新放大。"""
        signature = (
            theme.daily_theme_date,
            (theme.special_date_theme or {}).get("id"),
            bool(theme.theme_config_valid),
        )
        if signature == self._last_theme_metric_signature:
            return
        self._last_theme_metric_signature = signature
        if theme.special_date_theme:
            self._stats["special_date_theme_hits"] += 1
        if not theme.theme_config_valid:
            self._stats["theme_config_degraded"] += 1

    def _ensure_activity_service(self) -> StreamerActivityService:
        if self._activity_service is None:
            from kangel.infrastructure.database import db_manager
            self._activity_service = StreamerActivityService(
                db_manager, settings.stream.activity_candidates
            )
        return self._activity_service

    def _ensure_beat_scheduler(self) -> StreamerBeatScheduler:
        if self._beat_scheduler is None:
            from kangel.infrastructure.database import db_manager
            self._beat_scheduler = StreamerBeatScheduler(db_manager)
        return self._beat_scheduler

    def _ensure_mainline_service(self) -> StreamMainlineService:
        if self._mainline_service is None:
            activity_service = self._ensure_activity_service()
            plan_service = DailyStreamPlanService(activity_service.candidates)
            self._mainline_service = StreamMainlineService(
                activity_service.database, plan_service
            )
        return self._mainline_service

    def _ensure_director_runtime(self) -> StreamDirectorRuntime:
        if self._director_runtime is None:
            mainline = self._ensure_mainline_service()
            activity = self._ensure_activity_service()
            ai_candidate = None
            if settings.stream.director_mode in {"ai_shadow", "ai"}:
                from kangel.integrations.ai.stream_director import AIStreamDirectorCandidate
                ai_candidate = AIStreamDirectorCandidate()
            self._director_runtime = StreamDirectorRuntime(
                context_provider=self._build_director_context,
                executor=StreamerActionExecutor(mainline, activity),
                on_committed=self._on_director_committed,
                on_performance=self._on_director_performance,
                ai_candidate=ai_candidate,
            )
        return self._director_runtime

    async def _maybe_emit_streamer_beat(self) -> None:
        """独立微动作仅使用运行快照；阻塞主链路时安静跳过。"""
        if not settings.stream.beat_enabled:
            return
        scheduler = self._ensure_beat_scheduler()
        try:
            from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
            from kangel.infrastructure.event_bus import persona_event_pipeline
            from kangel.integrations.superchat.service import sc_service

            sc_pending = await asyncio.to_thread(sc_service.has_active_work)
            gate = ai_reply_work_gate.snapshot()
            beat = await scheduler.tick({
                "is_live": self._metadata.is_live,
                "stream_session_id": self.get_current_stream_session_id(),
                "activity": self._metadata.current_activity,
                "danmaku_rate": persona_event_pipeline.current_danmaku_rate,
                "sc_pending": sc_pending,
                "ai_waiting": bool(gate["active"] or gate["waiting"]),
                "slow_consumer": self._has_slow_subscriber(),
                "activity_switch_recent": (
                    time.monotonic() - self._last_activity_transition_at
                    < settings.stream.beat_min_interval_seconds
                ),
            })
        except Exception as exc:
            logger.warning("主播节拍评估已跳过: %s", exc)
            return
        if not beat:
            return
        self._stats["streamer_beat_emitted"] += 1
        await self._broadcast_to_all({
            "type": WebSocketEventType.STREAMER_BEAT,
            "data": beat.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _has_slow_subscriber(self) -> bool:
        """节拍是可丢弃演出；任意订阅客户端输出拥塞时不额外施压。"""
        if not self._subscribers:
            return False
        from kangel.transport.websocket.connection_manager import connection_manager

        threshold = settings.stream.beat_slow_consumer_queue_threshold
        return any(
            connection.websocket in self._subscribers
            and connection.send_queue.qsize() >= threshold
            for connection in connection_manager.active_connections.values()
        )

    def _ensure_intent_service(self):
        if self._intent_service is None:
            from kangel.infrastructure.database import db_manager
            from kangel.persona.application.intent_state import StreamerIntentStateService
            self._intent_service = StreamerIntentStateService(db_manager)
        return self._intent_service

    def _ensure_session_summary_service(self):
        if self._session_summary_service is None:
            from .session_summary import StreamSessionSummaryService
            self._session_summary_service = StreamSessionSummaryService(
                self._ensure_activity_service().database
            )
        return self._session_summary_service

    def _refresh_session_summary(self, schedule, theme) -> None:
        """按排期收口 P21 与 P24；两条记忆链路各自可关闭且互不依赖。"""
        frozen_sessions: list[str] = []
        if settings.session_summary.capture_enabled:
            from kangel.persona.application.engine import persona_engine

            if (
                self._current_mainline
                and self._current_mainline.stream_session_id
                == schedule.current_stream_start_time
                and self._current_mainline.theme_snapshot
            ):
                theme_context = dict(self._current_mainline.theme_snapshot)
            else:
                theme_reference = (
                    datetime.fromisoformat(schedule.current_stream_start_time)
                    if schedule.is_live and schedule.current_stream_start_time else None
                )
                try:
                    theme_context = self._theme.prompt_context(theme_reference)
                except TypeError:
                    # 兼容测试和第三方 Theme provider 的旧无参数契约；这里不能调用
                    # get_theme_prompt_context()，否则会从 session summary 反向刷新排期。
                    theme_context = self._theme.prompt_context()

            service = self._ensure_session_summary_service()
            if (
                schedule.is_live
                and schedule.current_stream_start_time
                and schedule.current_stream_end_time
            ):
                service.open_session(
                    stream_session_id=schedule.current_stream_start_time,
                    scheduled_start_at=schedule.current_stream_start_time,
                    scheduled_end_at=schedule.current_stream_end_time,
                    schedule_timezone=schedule.schedule_timezone,
                    theme=theme_context,
                    persona_state=persona_engine.state.model_dump(),
                )
            frozen_sessions = service.reconcile_closed_sessions(
                closure_context=self._build_session_summary_closure_context
            )
        try:
            from kangel.memory.application.episodic import episodic_memory_manager
            if settings.episodic_memory.enabled:
                current_session_id = (
                    schedule.current_stream_start_time if schedule.is_live else None
                )
                # P24 不依赖 P21 的事实表：重启、关闭 P21 或旧场次缺少
                # public facts 时，仍按候选的场次 ID 独立创建一次任务。
                candidate_sessions = episodic_memory_manager.database.list_stream_memory_candidate_sessions(
                    exclude_stream_session_id=current_session_id
                )
                frozen_sessions = list(dict.fromkeys(frozen_sessions + candidate_sessions))
            for session_id in frozen_sessions:
                episodic_memory_manager.freeze_session(session_id)
        except Exception as exc:
            logger.warning("创建 P24 下播情景记忆任务失败: %s", exc)

    def _build_session_summary_closure_context(self, stream_session_id: str) -> dict:
        """构造最小必要的公共闭场事实，禁止带入原始互动或身份信息。"""
        from kangel.persona.application.engine import persona_engine
        from kangel.infrastructure.event_bus import persona_event_pipeline

        transitions = self._ensure_activity_service().list_transitions(
            stream_session_id, limit=12
        )
        return {
            "activity_timeline": transitions,
            "persona_state": persona_engine.state.model_dump(),
            "viewer_count": self._metadata.viewer_count,
            "danmaku_rate": persona_event_pipeline.current_danmaku_rate,
            "audience_sentiment": persona_event_pipeline.audience_sentiment,
            "audience_sample_count": (
                persona_event_pipeline.audience_sentiment_sample_count
            ),
        }

    def _refresh_activity(self, schedule, theme) -> None:
        service = self._ensure_activity_service()
        self._metadata.activity_config_valid = not service.errors
        self._metadata.activity_errors = list(service.errors)
        now = datetime.now(timezone.utc).isoformat()
        if schedule.is_live and schedule.current_stream_start_time:
            session_id = schedule.current_stream_start_time
            frozen_mainline, _ = self._load_mainline(
                self._ensure_mainline_service(), session_id
            )
            service.end_other_sessions(session_id, now)
            intent_service = self._ensure_intent_service()
            intent_service.expire_other_sessions(
                session_id, now=datetime.now(timezone.utc)
            )
            intent_service.get_or_create(session_id, now=datetime.now(timezone.utc))
            was_missing = service.get(session_id) is None
            # 特殊日期只有显式配置 activity_theme_id 时才影响本场初始候选；
            # 已持久化的活动继续是唯一事实，不会因跨日或重启被改写。
            initialization_theme_id = (
                frozen_mainline.theme_id if frozen_mainline
                else theme.activity_theme_id or theme.daily_theme_id
            )
            continuity_activity_id = None
            if was_missing and settings.session_summary.capture_enabled:
                continuity_activity_id = (
                    self._ensure_session_summary_service().get_activity_initialization_hint(
                        current_stream_session_id=session_id
                    )
                )
            self._current_activity = service.get_or_create(
                stream_session_id=session_id,
                theme_id=initialization_theme_id,
                started_at=schedule.current_stream_start_time,
                continuity_activity_id=continuity_activity_id,
            )
            if was_missing:
                # 初始活动也是本场值得记住的事实；只写 P24 候选，不触发
                # 额外模型调用，也不把初始化动作当作公开切换广播。
                try:
                    from kangel.memory.application.episodic import episodic_memory_manager
                    transition = service.list_transitions(session_id, limit=1)
                    if transition:
                        episodic_memory_manager.capture_activity(
                            stream_session_id=session_id, transition=transition[0]
                        )
                except Exception as exc:
                    logger.debug("记录 P24 初始活动候选失败: %s", exc)
            if was_missing:
                self._stats["activity_initializations"] += 1
                if continuity_activity_id == self._current_activity.activity_id:
                    self._stats.setdefault("summary_activity_continuity_applied", 0)
                    self._stats["summary_activity_continuity_applied"] += 1
            self._refresh_stream_affect_anchor(session_id, theme)
            self._maybe_evaluate_activity(
                frozen_mainline.theme_id if frozen_mainline else theme.daily_theme_id
            )
            state = self._current_activity
            self._metadata.current_activity = {
                "activity_id": state.activity_id,
                "category": state.category,
                "display_name": state.display_name,
                "object_name": state.object_name,
                "started_at": state.started_at,
                "version": state.version,
            }
        else:
            service.end_other_sessions(None, now)
            self._ensure_intent_service().expire_other_sessions(
                None, now=datetime.now(timezone.utc)
            )
            self._current_activity = None
            self._metadata.current_activity = None
            from kangel.persona.application.engine import persona_engine
            persona_engine.clear_stream_affect_anchor()

    def _load_mainline(
        self, service: StreamMainlineService, session_id: str
    ) -> tuple[Optional[StreamMainlineState], list[str]]:
        """恢复本场主线；快照不可读时降级，绝不把异常抛回回复热路径。

        调用链是 get_theme_prompt_context() → _refresh_schedule() →
        这里，AI 回复每条都会经过，所以恢复失败只能变成一个错误字段。
        """
        if not settings.stream.mainline_enabled:
            return None, []
        try:
            return service.get(session_id), []
        except Exception as exc:
            self._stats.setdefault("mainline_restore_failures", 0)
            self._stats["mainline_restore_failures"] += 1
            logger.warning(
                "主线快照恢复失败，本场按无主线降级: session=%s error=%s", session_id, exc
            )
            return None, [f"mainline_snapshot_unreadable: {exc}"]

    def _clear_mainline_metadata(self, session_id: Optional[str] = None) -> None:
        self._current_mainline = None
        self._metadata.stream_session_id = session_id
        self._metadata.session_theme = None
        self._metadata.daily_stream_plan = None
        self._metadata.current_mainline_beat = None
        self._metadata.mainline_config_valid = True
        self._metadata.mainline_errors = []

    def _refresh_mainline(self, schedule, theme) -> None:
        """创建/恢复不可变 Plan 与当前 Mainline Beat；不执行 Director。"""
        if not settings.stream.mainline_enabled:
            self._clear_mainline_metadata()
            return
        service = self._ensure_mainline_service()
        now = datetime.now(timezone.utc).isoformat()
        if not schedule.is_live or not schedule.current_stream_start_time:
            service.end_other_sessions(None, now)
            self._clear_mainline_metadata()
            return
        if not self._current_activity:
            return
        session_id = schedule.current_stream_start_time
        service.end_other_sessions(session_id, now)
        existing, restore_errors = self._load_mainline(service, session_id)
        if restore_errors:
            # 本场不再推进主线也不注入 Prompt，但弹幕/SC/AI 主链路照常。
            self._clear_mainline_metadata(session_id)
            self._metadata.mainline_config_valid = False
            self._metadata.mainline_errors = restore_errors
            return
        errors: list[str] = []
        if existing:
            # 活跃场次永远读取冻结快照；配置热更新不重新校验或覆盖本场 Plan。
            self._current_mainline = existing
        else:
            get_plan = getattr(self._theme, "get_stream_plan_config", None)
            raw_plan = get_plan(theme.daily_theme_id) if callable(get_plan) else None
            theme_reference = datetime.fromisoformat(schedule.current_stream_start_time)
            try:
                theme_snapshot = self._theme.prompt_context(theme_reference)
            except TypeError:
                theme_snapshot = self._theme.prompt_context()
            theme_snapshot = {
                **dict(theme_snapshot),
                "id": theme.daily_theme_id,
                "name": theme.daily_theme_name,
                "date": theme.daily_theme_date,
            }
            plan, errors = service.plan_service.build(
                theme_id=theme.daily_theme_id,
                theme_name=theme.daily_theme_name,
                raw_plan=raw_plan,
                initial_activity_id=self._current_activity.activity_id,
            )
            self._current_mainline = service.get_or_create(
                stream_session_id=session_id,
                theme_id=theme.daily_theme_id,
                theme_date=theme.daily_theme_date,
                special_theme_id=(theme.special_date_theme or {}).get("id"),
                theme_snapshot=theme_snapshot,
                plan=plan,
                started_at=schedule.current_stream_start_time,
            )
        state = self._current_mainline
        self._metadata.stream_session_id = session_id
        self._metadata.session_theme = {
            "id": state.theme_id,
            "name": state.theme_snapshot.get("name") or theme.daily_theme_name,
            "date": state.theme_date,
        }
        # 旧字段在直播中也投影同一份冻结事实，避免跨午夜或配置热更新后
        # daily_theme_* 与 session_theme 自相矛盾；这是对既有公开字段的
        # 语义变更，因此按灰度开关控制，默认仍跟随实时配置。
        if settings.stream.mainline_theme_projection_enabled:
            self._metadata.daily_theme_id = state.theme_id
            self._metadata.daily_theme_name = (
                state.theme_snapshot.get("name") or theme.daily_theme_name
            )
            self._metadata.daily_theme_date = state.theme_date
        self._metadata.daily_stream_plan = state.public_plan()
        self._metadata.current_mainline_beat = state.public_beat()
        self._metadata.mainline_config_valid = not errors
        self._metadata.mainline_errors = errors

    def _refresh_stream_affect_anchor(self, session_id: str, theme) -> None:
        """建立/恢复本场锚点，并只消费房间聚合的低幅长期信号。"""
        special = theme.special_date_theme
        bias = theme.special_mood_bias
        service = self._ensure_activity_service()
        from kangel.persona.application.engine import persona_engine
        from kangel.infrastructure.event_bus import persona_event_pipeline
        activity = self._current_activity
        anchor = persona_engine.refresh_stream_affect_anchor(
            session_id,
            mood_bias=bias,
            sources={
                "daily_theme_id": theme.daily_theme_id,
                "special_theme_id": (special or {}).get("id"),
                "activity_id": activity.activity_id if activity else None,
            },
            audience_sentiment=persona_event_pipeline.audience_sentiment,
            room_sample_count=persona_event_pipeline.audience_sentiment_sample_count,
            danmaku_rate=persona_event_pipeline.current_danmaku_rate,
        )
        if special and bias and service.database.claim_stream_special_date_bias(
            session_id, special["id"], datetime.now(timezone.utc).isoformat()
        ):
            self._stats.setdefault("special_date_bias_applied", 0)
            self._stats["special_date_bias_applied"] += 1
            logger.info(
                "特殊日期人格 bias 已写入场次锚点: session=%s theme=%s anchor_version=%s",
                session_id, special["id"], anchor.version if anchor else "unknown",
            )

    def _refresh_idle_state(self, schedule, theme) -> None:
        """基于读模型更新待机外显；不改写人格或活动事实。"""
        # 延迟导入避免 metadata 与人格装配模块形成导入环。
        from kangel.persona.application.engine import persona_engine
        from kangel.infrastructure.event_bus import persona_event_pipeline

        candidate = self._idle_state_resolver.resolve(
            is_live=schedule.is_live,
            special_date_theme=theme.special_date_theme,
            special_idle_state_hint=theme.special_idle_state_hint,
            daily_theme_name=theme.daily_theme_name,
            current_activity=self._metadata.current_activity,
            persona_state=persona_engine.state,
            internal_state=persona_engine.internal_state,
            audience_sentiment=persona_event_pipeline.audience_sentiment,
        )
        now = time.monotonic()
        if self._idle_state and candidate == self._idle_state:
            return
        fact_signature = (
            schedule.is_live,
            schedule.current_stream_start_time,
            (theme.special_date_theme or {}).get("id"),
            theme.special_idle_state_hint,
            (self._metadata.current_activity or {}).get("activity_id"),
            (self._metadata.current_activity or {}).get("version"),
        )
        immediate = (
            self._idle_state is None
            or fact_signature != self._idle_fact_signature
        )
        if (
            not immediate
            and now - self._idle_state_changed_at
            < settings.stream.idle_state_min_duration_seconds
        ):
            return
        self._idle_state = candidate
        self._idle_fact_signature = fact_signature
        self._idle_state_version += 1
        self._idle_state_changed_at = now
        self._metadata.streamer_idle_state = self._public_idle_state(candidate)
        self._stats["idle_state_changes"] += 1
        reason_group = {
            "offline": "offline",
            "special_date": "special_date",
            "current_activity": "current_activity",
            "darkness": "persona",
            "stress": "persona",
            "fatigue": "persona",
            "arousal": "persona",
            "attachment": "persona",
            "default": "default",
        }.get(candidate.reason, "default")
        self._stats[f"idle_reason_{reason_group}"] += 1
        logger.info(
            "主播待机状态更新: state=%s version=%s reason=%s",
            candidate.idle_state, self._idle_state_version, candidate.reason,
        )
        self._schedule_idle_state_event()

    def _public_idle_state(self, state: IdleState) -> dict:
        return {
            "idle_state": state.idle_state,
            "idle_text": state.idle_text,
            "frontend_animation": state.frontend_animation,
            "background_music_hint": state.background_music_hint,
            "priority": state.priority,
            "version": self._idle_state_version,
        }

    def _schedule_idle_state_event(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._broadcast_idle_state())

    async def _broadcast_idle_state(self) -> None:
        if not self._metadata.streamer_idle_state:
            return
        await self._broadcast_to_all({
            "type": WebSocketEventType.STREAMER_IDLE_STATE,
            "data": self._metadata.streamer_idle_state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _maybe_evaluate_activity(self, theme_id: str) -> None:
        if not self._current_activity:
            return
        monotonic_now = time.monotonic()
        if (
            monotonic_now - self._last_activity_evaluation_at
            < settings.stream.activity_evaluation_interval_seconds
        ):
            return
        self._last_activity_evaluation_at = monotonic_now
        # 延迟导入避免活动事实层与人格/事件流水线形成模块循环。
        from kangel.persona.application.engine import persona_engine
        from kangel.infrastructure.event_bus import persona_event_pipeline
        now = datetime.now(timezone.utc)
        allow_public = (
            settings.stream.activity_public_performance_enabled
            and self._activity_service.public_performance_allowed(
                self._current_activity.stream_session_id,
                now,
                settings.stream.activity_public_performance_min_interval_minutes,
                settings.stream.activity_public_performance_max_per_stream,
            )
        )
        previous = self._current_activity
        proposal = self._activity_service.propose_switch(
            current=self._current_activity,
            theme_id=theme_id,
            now=now,
            mood=persona_engine.state.mood,
            stress=persona_engine.state.stress,
            fatigue=persona_engine.internal_state.fatigue,
            danmaku_rate=persona_event_pipeline.current_danmaku_rate,
            switch_cooldown_minutes=settings.stream.activity_switch_cooldown_minutes,
            max_duration_minutes=settings.stream.activity_max_duration_minutes,
            busy_rate_threshold=settings.stream.activity_busy_rate_threshold,
            allow_public_performance=allow_public,
            darkness=persona_engine.state.darkness,
            arousal=persona_engine.internal_state.arousal,
            audience_sentiment=persona_event_pipeline.audience_sentiment,
        )
        self._last_activity_proposal = (
            {**proposal, "base_activity_version": previous.version}
            if proposal else None
        )
        if proposal and self._director_runtime:
            self._director_runtime.observe_legacy_activity_proposal(
                base_activity_version=previous.version, proposal=proposal
            )
        if settings.stream.director_enabled and self._director_runtime:
            try:
                asyncio.get_running_loop().create_task(
                    self._director_runtime.notify("activity_evaluation", priority=3)
                )
            except RuntimeError:
                pass
        # 切到 director driver 之前，旧 evaluator 继续拥有稳定运行路径。
        if (
            settings.stream.director_enabled
            and settings.stream.director_activity_driver == "director"
            and settings.stream.director_mode in {"deterministic", "ai"}
        ):
            return
        changed = None
        if proposal:
            changed = self._activity_service.switch_to_candidate(
                current=previous,
                candidate=proposal["candidate"],
                changed_at=proposal["changed_at"],
                trigger_source=proposal["reason_code"],
                public_performance=proposal["public_performance"],
            )
        if changed:
            if self._director_runtime:
                self._director_runtime.observe_legacy_activity_commit(changed)
            logger.info(
                "主播活动%s切换: session=%s activity=%s version=%s reason=%s",
                "公开" if changed.public_performance else "静默",
                changed.stream_session_id,
                changed.activity_id,
                changed.version,
                changed.trigger_source,
            )
            self._current_activity = changed
            self._record_activity_transition(previous, changed)

    async def consider_activity_suggestion(
        self, *, message: str, identity, relationship, sentiment: float,
        danmaku_rate: int,
    ) -> bool:
        """登录观众的明确建议经过关系、节奏和版本门槛后才可切换。"""
        self._refresh_schedule()
        if (
            not self._current_activity or not self._metadata.is_live
            or not identity or not identity.is_authenticated
        ):
            self._stats["activity_suggestions_suppressed"] += 1
            return False
        now = datetime.now(timezone.utc)
        allow_public = (
            settings.stream.activity_public_performance_enabled
            and self._activity_service.public_performance_allowed(
                self._current_activity.stream_session_id,
                now,
                settings.stream.activity_public_performance_min_interval_minutes,
                settings.stream.activity_public_performance_max_per_stream,
            )
        )
        previous = self._current_activity
        changed = self._activity_service.suggest_from_danmaku(
            current=previous,
            message=message,
            now=now,
            familiarity=relationship.familiarity,
            trust=relationship.trust,
            sentiment=sentiment,
            danmaku_rate=danmaku_rate,
            min_familiarity=settings.stream.activity_suggestion_min_familiarity,
            min_trust=settings.stream.activity_suggestion_min_trust,
            switch_cooldown_minutes=settings.stream.activity_switch_cooldown_minutes,
            busy_rate_threshold=settings.stream.activity_busy_rate_threshold,
            allow_public_performance=allow_public,
        )
        if not changed:
            self._stats["activity_suggestions_suppressed"] += 1
            return False
        self._current_activity = changed
        self._metadata.current_activity = self._public_activity(changed)
        self._stats["activity_suggestions_accepted"] += 1
        self._record_activity_transition(previous, changed)
        return True

    def _record_activity_transition(
        self, previous: StreamerActivityState, changed: StreamerActivityState
    ) -> None:
        self._last_activity_transition_at = time.monotonic()
        if self._director_runtime:
            try:
                asyncio.get_running_loop().create_task(
                    self._director_runtime.notify("activity_transition", priority=2)
                )
            except RuntimeError:
                pass
        try:
            from kangel.memory.application.episodic import episodic_memory_manager
            transition = self._ensure_activity_service().list_transitions(
                changed.stream_session_id, limit=1
            )
            if transition:
                episodic_memory_manager.capture_activity(
                    stream_session_id=changed.stream_session_id,
                    transition=transition[0],
                )
        except Exception as exc:
            logger.debug("记录 P24 活动候选失败: %s", exc)
        if changed.public_performance:
            if self._schedule_activity_event(previous, changed):
                self._stats["activity_public_switches"] += 1
        else:
            self._stats["activity_silent_switches"] += 1

    def _build_director_context(self) -> Optional[dict[str, Any]]:
        if (
            not self._metadata.is_live or not self._current_mainline
            or not self._current_activity
        ):
            return None
        from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
        from kangel.integrations.superchat.service import sc_service
        from kangel.persona.application.engine import persona_engine

        now = datetime.now(timezone.utc)
        gate = ai_reply_work_gate.snapshot()
        busy = bool(
            gate["active"] or gate["waiting"] or sc_service.has_active_work()
            or self._has_slow_subscriber()
        )
        remaining = None
        if self._metadata.current_stream_end_time:
            end = datetime.fromisoformat(self._metadata.current_stream_end_time)
            remaining = max(0.0, (end - now.astimezone(end.tzinfo or timezone.utc)).total_seconds())
        activity_candidate = None
        if self._director_runtime:
            signals = self._director_runtime.signals.snapshot(now)
            activity_candidate = self._activity_service.propose_switch(
                current=self._current_activity,
                theme_id=self._current_mainline.theme_id,
                now=now,
                mood=persona_engine.state.mood,
                stress=persona_engine.state.stress,
                fatigue=persona_engine.internal_state.fatigue,
                danmaku_rate=signals.rate_60s,
                switch_cooldown_minutes=settings.stream.activity_switch_cooldown_minutes,
                max_duration_minutes=settings.stream.activity_max_duration_minutes,
                busy_rate_threshold=settings.stream.activity_busy_rate_threshold,
                allow_public_performance=False,
                darkness=persona_engine.state.darkness,
                arousal=persona_engine.internal_state.arousal,
                audience_sentiment=signals.audience_sentiment,
            )
        return {
            "is_live": True,
            "now": now,
            "remaining_seconds": remaining,
            "mainline": self._current_mainline,
            "activity": self._current_activity,
            "activity_candidate": activity_candidate,
            "eligible_activities": (
                [dict(item) for item in self._activity_service.candidates]
                if (
                    settings.stream.director_activity_driver == "director"
                    or settings.stream.director_mode in {"shadow", "ai_shadow"}
                ) else []
            ),
            "mood": persona_engine.state.mood,
            "stress": persona_engine.state.stress,
            "darkness": persona_engine.state.darkness,
            "fatigue": persona_engine.internal_state.fatigue,
            "arousal": persona_engine.internal_state.arousal,
            "viewer_count": self._metadata.viewer_count,
            "busy": busy,
        }

    async def _on_director_committed(
        self, result: ActionExecutionResult, decision: StreamerActionDecision
    ) -> None:
        previous_activity = self._current_activity
        previous_beat_version = (
            self._current_mainline.beat_version if self._current_mainline else 0
        )
        self._current_mainline = result.mainline
        self._current_activity = result.activity
        self._metadata.daily_stream_plan = result.mainline.public_plan()
        self._metadata.current_mainline_beat = result.mainline.public_beat()
        self._metadata.current_activity = self._public_activity(result.activity)
        if result.mainline.beat_version > previous_beat_version:
            await self._broadcast_to_all({
                "type": WebSocketEventType.STREAM_MAINLINE_BEAT,
                "data": {
                    "stream_session_id": result.mainline.stream_session_id,
                    "plan_version": result.mainline.plan_version,
                    "beat": result.mainline.public_beat(),
                    "activity_version": result.activity.version,
                    "trigger_source": "stream_director",
                    "reason_code": decision.reason_code,
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        if previous_activity and result.activity.version > previous_activity.version:
            self._record_activity_transition(previous_activity, result.activity)
            await self._broadcast_activity_fact(previous_activity, result.activity)

    async def _broadcast_activity_fact(
        self, previous: StreamerActivityState, changed: StreamerActivityState
    ) -> None:
        await self._broadcast_to_all({
            "type": WebSocketEventType.STREAMER_ACTIVITY,
            "data": {
                "stream_session_id": changed.stream_session_id,
                "version": changed.version,
                "previous": self._public_activity(previous),
                "current": self._public_activity(changed),
                "trigger_type": "director_fact",
                "changed_at": changed.started_at,
                "emotions": [],
                "sentences": [],
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def _on_director_performance(
        self, action: PerformanceAction, context: dict[str, Any]
    ) -> bool:
        # Performance 是可丢弃出口，提交后仍要重新检查关键回复是否开始。
        latest = self._build_director_context()
        if not latest or latest.get("busy"):
            return False
        mainline: StreamMainlineState = latest["mainline"]
        if action.type == "SPEAK":
            reply = self._director_templates.render(
                action, stress=float(latest.get("stress", 0.0)),
                mood=float(latest.get("mood", 0.5)),
                darkness=float(latest.get("darkness", 0.0)),
                session_id=mainline.stream_session_id,
                version=mainline.beat_version,
            )
            if not reply:
                return False
            if (
                settings.stream.director_ai_speak_polish_enabled
                and self._director_runtime
                and self._director_runtime.ai_candidate is not None
            ):
                sentence = reply["sentences"][0]
                polished = await self._director_runtime.ai_candidate.polish_speech(
                    template_text=sentence["text"], emotion=sentence["emotion"],
                    context=latest,
                )
                after_polish = self._build_director_context()
                if not after_polish or after_polish.get("busy"):
                    return False
                if (
                    after_polish["mainline"].beat_version != mainline.beat_version
                    or after_polish["activity"].version != latest["activity"].version
                ):
                    return False
                if polished:
                    sentence["text"] = polished
            await self._broadcast_to_all({
                "type": WebSocketEventType.AI_REPLY,
                "data": {
                    "source": "stream_director",
                    "stream_session_id": mainline.stream_session_id,
                    "beat_version": mainline.beat_version,
                    "reply": reply,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return True
        elif action.type == "PLAY_ANIMATION" and action.animation_id:
            animation_text = {
                "stretch": "稍微活动一下……",
                "short_pause": "先停一下。",
                "celebrate": "好耶！",
                "glance_chat": "看看弹幕。",
            }.get(action.animation_id)
            if not animation_text:
                return False
            await self._broadcast_to_all({
                "type": WebSocketEventType.STREAMER_BEAT,
                "data": {
                    "source": "stream_director",
                    "event_id": str(__import__("uuid").uuid4()),
                    "stream_session_id": mainline.stream_session_id,
                    "animation_id": action.animation_id,
                    "beat_type": "director_animation",
                    "display_text": animation_text,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return True
        return False

    def _schedule_activity_event(
        self, previous: StreamerActivityState, changed: StreamerActivityState
    ) -> bool:
        key = changed.stream_session_id
        if self._last_activity_event_version.get(key, 0) >= changed.version:
            return False
        self._last_activity_event_version[key] = changed.version
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True
        loop.create_task(self._broadcast_activity_transition(previous, changed))
        return True

    async def _broadcast_activity_transition(
        self, previous: StreamerActivityState, changed: StreamerActivityState
    ) -> None:
        if changed.trigger_source == "audience_suggestion":
            emotion = "兴奋"
            text = f"好啦，既然大家想看，那接下来就换成{changed.display_name}——{changed.object_name}！"
            trigger_type = "audience_influenced"
        else:
            emotion = "思考"
            text = f"这个环节差不多啦，接下来做{changed.display_name}——{changed.object_name}。"
            trigger_type = "time_driven"
        await self._broadcast_to_all({
            "type": WebSocketEventType.STREAMER_ACTIVITY,
            "data": {
                "stream_session_id": changed.stream_session_id,
                "version": changed.version,
                "previous": self._public_activity(previous),
                "current": self._public_activity(changed),
                "trigger_type": trigger_type,
                "changed_at": changed.started_at,
                "emotions": [emotion],
                "sentences": [{"emotion": emotion, "text": text}],
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    @staticmethod
    def _public_activity(state: StreamerActivityState) -> dict:
        return {
            "activity_id": state.activity_id,
            "category": state.category,
            "display_name": state.display_name,
            "object_name": state.object_name,
            "started_at": state.started_at,
            "version": state.version,
        }

    def get_theme_prompt_context(self) -> dict:
        """直播中返回按场次冻结的主题；下播时返回当前自然日主题。"""
        self._refresh_schedule()
        if (
            self._metadata.is_live and self._current_mainline
            and self._current_mainline.theme_snapshot
        ):
            # 返回副本，调用方不能修改本场冻结事实。
            return json.loads(json.dumps(
                self._current_mainline.theme_snapshot, ensure_ascii=False
            ))
        reference = None
        if self._metadata.is_live and self._metadata.current_stream_start_time:
            reference = datetime.fromisoformat(self._metadata.current_stream_start_time)
        try:
            return self._theme.prompt_context(reference)
        except TypeError:
            return self._theme.prompt_context()

    def get_mainline_prompt_context(self) -> Optional[dict]:
        """返回紧凑、已提交的 Plan/Beat 事实，不暴露完整计划图。"""
        self._refresh_schedule()
        if not self._current_mainline:
            return None
        return self._ensure_mainline_service().prompt_context(self._current_mainline)

    def get_activity_prompt_context(self) -> Optional[dict]:
        """返回服务端确认的当前活动事实；下播时不提供。"""
        self._refresh_schedule()
        if not self._current_activity:
            return None
        state = self._current_activity
        return {
            "activity_id": state.activity_id,
            "category": state.category,
            "display_name": state.display_name,
            "object_name": state.object_name,
            "started_at": state.started_at,
            "version": state.version,
        }

    def get_current_stream_session_id(self) -> Optional[str]:
        """返回当前排期场次 ID；该字段也作为公开恢复快照的一部分。"""
        self._refresh_schedule()
        return self._metadata.stream_session_id

    def get_previous_session_summary_prompt_context(self, message: str) -> Optional[dict]:
        """返回上一场已完成总结的低权重背景；不会改写当前活动或人格事实。"""
        self._refresh_schedule()
        session_id = self.get_current_stream_session_id()
        if not session_id or not settings.session_summary.capture_enabled:
            return None
        return self._ensure_session_summary_service().build_reply_context(
            current_stream_session_id=session_id,
            message=message,
            current_activity=self.get_activity_prompt_context(),
            prompt_chars=settings.session_summary.prompt_chars,
        )

    def reply_preserves_activity_fact(self, reply_data: dict) -> bool:
        """拒绝模型在未提交版本切换时擅自宣布换到其他目录活动。"""
        if not self._current_activity or not isinstance(reply_data, dict):
            return True
        text = "".join(
            str(item.get("text", ""))
            for item in reply_data.get("sentences", []) if isinstance(item, dict)
        ).casefold()
        compact = "".join(text.split())
        if not any(word in compact for word in ("换成", "接下来玩", "接下来做", "不玩了", "改玩")):
            return True
        service = self._ensure_activity_service()
        conflicting = [
            item for item in service.candidates
            if item["id"] != self._current_activity.activity_id
            and any("".join(value.casefold().split()) in compact for value in (
                item["name"], item["object_name"]
            ))
        ]
        return not conflicting
    
    def get_recent_activities(self, limit: int = 20) -> List[UserActivity]:
        """获取最近的活动记录"""
        return self._user_activities[-limit:]
    
    def get_stats(self) -> dict:
        """获取推送统计"""
        self._refresh_schedule()
        session_summary_stats = {"disabled": 1}
        if settings.session_summary.capture_enabled:
            from .session_summary import stream_session_summary_consumer
            session_summary_stats = {
                **self._ensure_session_summary_service().get_stats(),
                "consumer": stream_session_summary_consumer.get_stats(),
            }
        return {
            **self._stats,
            "subscriber_count": len(self._subscribers),
            "is_running": self._running,
            "push_interval_ms": self._push_interval_ms,
            "enable_push": self._enable_push,
            "streamer_beat": self._ensure_beat_scheduler().get_stats(),
            "stream_director": (
                self._director_runtime.get_stats()
                if self._director_runtime else {"running": False, "mode": "disabled"}
            ),
            "session_summary": session_summary_stats,
            "current_metadata": self._metadata.to_dict()
        }


# 全局直播间元信息推送器实例
stream_metadata_pusher = StreamMetadataPusher()
