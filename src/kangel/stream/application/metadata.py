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
    current_activity: Optional[Dict[str, Any]] = None
    activity_config_valid: bool = True
    activity_errors: List[str] = field(default_factory=list)
    
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
            schedule_zone, settings.stream.daily_themes
        )
        self._activity_service = activity_service
        self._activity_from_settings = activity_service is None
        self._current_activity: Optional[StreamerActivityState] = None
        self._last_activity_evaluation_at = 0.0
        self._last_activity_event_version: dict[str, int] = {}
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
                self._schedule.zone, settings.stream.daily_themes
            )
        self._ensure_activity_service()
        
        self._running = True
        self._refresh_schedule()
        self._stats["start_time"] = datetime.now().isoformat()
        self._push_task = asyncio.create_task(self._push_loop())
        
        logger.info(f"🚀 直播间元信息推送服务启动，推送间隔: {self._push_interval_ms}ms")
    
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
        
        logger.info("🛑 直播间元信息推送服务已停止")
    
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
                "current_activity": self._metadata.current_activity,
                "activity_config_valid": self._metadata.activity_config_valid,
                "activity_errors": self._metadata.activity_errors,
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
            (self._metadata.current_activity or {}).get("version"),
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
        theme = self._theme.evaluate()
        self._metadata.daily_theme_id = theme.daily_theme_id
        self._metadata.daily_theme_name = theme.daily_theme_name
        self._metadata.daily_theme_date = theme.daily_theme_date
        self._metadata.theme_config_valid = theme.theme_config_valid
        self._metadata.theme_errors = theme.theme_errors
        self._refresh_activity(snapshot, theme)
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
            (self._metadata.current_activity or {}).get("version"),
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
        return changed

    def _ensure_activity_service(self) -> StreamerActivityService:
        if self._activity_service is None:
            from kangel.infrastructure.database import db_manager
            self._activity_service = StreamerActivityService(
                db_manager, settings.stream.activity_candidates
            )
        return self._activity_service

    def _refresh_activity(self, schedule, theme) -> None:
        service = self._ensure_activity_service()
        self._metadata.activity_config_valid = not service.errors
        self._metadata.activity_errors = list(service.errors)
        now = datetime.now(timezone.utc).isoformat()
        if schedule.is_live and schedule.current_stream_start_time:
            session_id = schedule.current_stream_start_time
            service.end_other_sessions(session_id, now)
            was_missing = service.get(session_id) is None
            self._current_activity = service.get_or_create(
                stream_session_id=session_id,
                theme_id=theme.daily_theme_id,
                started_at=schedule.current_stream_start_time,
            )
            if was_missing:
                self._stats["activity_initializations"] += 1
            self._maybe_evaluate_activity(theme.daily_theme_id)
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
            self._current_activity = None
            self._metadata.current_activity = None

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
        changed = self._activity_service.evaluate_and_switch(
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
        if changed:
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
        if changed.public_performance:
            if self._schedule_activity_event(previous, changed):
                self._stats["activity_public_switches"] += 1
        else:
            self._stats["activity_silent_switches"] += 1

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
        """只向人格模块提供当前主题及后端专用点缀提示。"""
        self._refresh_schedule()
        return self._theme.prompt_context()

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
        return {
            **self._stats,
            "subscriber_count": len(self._subscribers),
            "is_running": self._running,
            "push_interval_ms": self._push_interval_ms,
            "enable_push": self._enable_push,
            "current_metadata": self._metadata.to_dict()
        }


# 全局直播间元信息推送器实例
stream_metadata_pusher = StreamMetadataPusher()
