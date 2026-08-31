"""克制的直播主线 Director：事件聚合、确定性决策与受控事实出口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from config import settings
from kangel.shared.logging import logger
from kangel.stream.application.activity import StreamerActivityService, StreamerActivityState
from kangel.stream.application.mainline import StreamMainlineService
from kangel.stream.domain.mainline import StreamMainlineState


FACT_TYPES = frozenset({"SET_MAINLINE_BEAT", "CHANGE_ACTIVITY"})
PERFORMANCE_TYPES = frozenset({"SPEAK", "PLAY_ANIMATION"})
TEMPLATE_FAMILIES = frozenset({
    "GAME_FATIGUE", "HIGH_STRESS", "RETURN_MAINLINE", "QUIET_GAME_COMMENTARY",
})
ANIMATION_IDS = frozenset({"stretch", "short_pause", "celebrate", "glance_chat"})


@dataclass(frozen=True)
class FactMutation:
    type: str
    target_beat_id: str | None = None
    target_activity_id: str | None = None


@dataclass(frozen=True)
class PerformanceAction:
    type: str
    template_family: str | None = None
    emotion_hint: str | None = None
    animation_id: str | None = None


@dataclass(frozen=True)
class StreamerActionDecision:
    decision: str
    reason_code: str
    stream_session_id: str
    plan_version: int
    beat_version: int
    activity_version: int
    fact_mutations: tuple[FactMutation, ...] = ()
    performance_actions: tuple[PerformanceAction, ...] = ()
    decision_source: str = "deterministic"
    schema_version: str = "streamer-action-decision-v1"

    @classmethod
    def continue_(
        cls, mainline: StreamMainlineState, activity: StreamerActivityState,
        reason_code: str = "NO_CHANGE",
    ) -> "StreamerActionDecision":
        return cls(
            decision="CONTINUE", reason_code=reason_code,
            stream_session_id=mainline.stream_session_id,
            plan_version=mainline.plan_version, beat_version=mainline.beat_version,
            activity_version=activity.version,
        )


@dataclass(frozen=True)
class DirectorSignalSnapshot:
    rate_60s: int
    rate_300s: int
    silence_seconds: float
    audience_sentiment: float
    sentiment_samples: int


class DirectorSignalTracker:
    """独立时间窗口信号；不会继承 persona pipeline 的陈旧速率。"""

    def __init__(self):
        self._danmaku_times: deque[datetime] = deque(maxlen=4096)
        self._sentiments: deque[tuple[datetime, float]] = deque(maxlen=256)
        self._last_activity_at: datetime | None = None

    def record_danmaku(
        self, *, sentiment: float = 0.0, occurred_at: datetime | None = None
    ) -> None:
        now = self._aware(occurred_at or datetime.now(timezone.utc))
        self._danmaku_times.append(now)
        self._sentiments.append((now, min(1.0, max(-1.0, float(sentiment)))))
        self._last_activity_at = now
        self._trim(now)

    def record_activity(self, occurred_at: datetime | None = None) -> None:
        self._last_activity_at = self._aware(occurred_at or datetime.now(timezone.utc))

    def snapshot(self, now: datetime | None = None) -> DirectorSignalSnapshot:
        reference = self._aware(now or datetime.now(timezone.utc))
        self._trim(reference)
        rate_60 = sum((reference - item).total_seconds() <= 60 for item in self._danmaku_times)
        rate_300 = sum((reference - item).total_seconds() <= 300 for item in self._danmaku_times)
        recent_sentiments = [
            value for occurred, value in self._sentiments
            if (reference - occurred).total_seconds() <= 300
        ]
        silence = (
            max(0.0, (reference - self._last_activity_at).total_seconds())
            if self._last_activity_at else float("inf")
        )
        return DirectorSignalSnapshot(
            rate_60s=rate_60, rate_300s=rate_300, silence_seconds=silence,
            audience_sentiment=(sum(recent_sentiments) / len(recent_sentiments)
                                if recent_sentiments else 0.0),
            sentiment_samples=len(recent_sentiments),
        )

    def observe_persona_event(self, event: Any, _snapshot: dict | None = None) -> None:
        name = event.__class__.__name__
        if name == "DanmakuReceivedEvent":
            self.record_danmaku(
                sentiment=float(getattr(event, "sentiment", 0.0)),
                occurred_at=getattr(event, "occurred_at", None),
            )
        elif name in {"GiftReceivedEvent", "ModerationActionEvent", "StreamLifecycleEvent"}:
            self.record_activity(getattr(event, "occurred_at", None))

    def _trim(self, now: datetime) -> None:
        while self._danmaku_times and (now - self._danmaku_times[0]).total_seconds() > 300:
            self._danmaku_times.popleft()
        while self._sentiments and (now - self._sentiments[0][0]).total_seconds() > 300:
            self._sentiments.popleft()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class DeterministicStreamDirector:
    """保守策略：没有明确理由时始终 CONTINUE。"""

    def evaluate(
        self, context: dict[str, Any], signals: DirectorSignalSnapshot
    ) -> StreamerActionDecision:
        mainline: StreamMainlineState = context["mainline"]
        activity: StreamerActivityState = context["activity"]
        now: datetime = context.get("now") or datetime.now(timezone.utc)
        beat_started = datetime.fromisoformat(mainline.beat_started_at)
        if beat_started.tzinfo is None:
            beat_started = beat_started.replace(tzinfo=now.tzinfo or timezone.utc)
        beat_seconds = max(0.0, (now - beat_started).total_seconds())
        remaining = context.get("remaining_seconds")
        activity_candidate = context.get("activity_candidate")

        closing = mainline.plan.beat(mainline.plan.closing_beat_id)
        if (
            closing and remaining is not None
            and 0 <= remaining <= settings.stream.director_wrap_up_seconds
            and mainline.current_beat_id != closing.beat_id
        ):
            return self._act(mainline, activity, "SCHEDULE_WRAP_UP", (
                FactMutation("SET_MAINLINE_BEAT", target_beat_id=closing.beat_id),
            ))

        if mainline.current_beat_kind == "opening" and beat_seconds >= settings.stream.director_opening_min_seconds:
            target = next((beat for beat in mainline.plan.beats if beat.kind == "mainline"), None)
            if target:
                return self._act(mainline, activity, "OPENING_COMPLETE", (
                    FactMutation("SET_MAINLINE_BEAT", target_beat_id=target.beat_id),
                ))

        compatible = set(mainline.current_beat.compatible_activity_ids) if mainline.current_beat else set()
        if activity.activity_id not in compatible:
            candidate_target = (
                (activity_candidate or {}).get("candidate", {}).get("id")
            )
            if (
                candidate_target in compatible
                and settings.stream.director_activity_driver == "director"
            ):
                return self._act(mainline, activity, "ACTIVITY_ALIGNMENT", (
                    FactMutation("CHANGE_ACTIVITY", target_activity_id=candidate_target),
                ))
            matches = [beat for beat in mainline.plan.beats if activity.activity_id in beat.compatible_activity_ids]
            target = next((beat for beat in matches if beat.kind == "mainline"), None)
            target = target or next((beat for beat in matches if beat.kind == "detour"), None)
            if target and target.beat_id != mainline.current_beat_id:
                return self._act(mainline, activity, "ACTIVITY_ALIGNMENT", (
                    FactMutation("SET_MAINLINE_BEAT", target_beat_id=target.beat_id),
                ))

        fatigue = float(context.get("fatigue", 0.0))
        stress = float(context.get("stress", 0.0))
        if mainline.current_beat_kind != "detour" and (fatigue >= 0.75 or stress >= 0.8):
            detour = next((beat for beat in mainline.plan.beats if beat.kind == "detour"), None)
            if detour:
                family = "GAME_FATIGUE" if fatigue >= 0.75 else "HIGH_STRESS"
                facts = [FactMutation("SET_MAINLINE_BEAT", target_beat_id=detour.beat_id)]
                candidate_target = (
                    (activity_candidate or {}).get("candidate", {}).get("id")
                )
                if (
                    settings.stream.director_activity_driver == "director"
                    and candidate_target in detour.compatible_activity_ids
                ):
                    facts.append(FactMutation(
                        "CHANGE_ACTIVITY", target_activity_id=candidate_target
                    ))
                return self._act(
                    mainline, activity, family,
                    tuple(facts),
                    (
                        PerformanceAction(
                            "SPEAK", template_family=family,
                            emotion_hint="疲惫" if fatigue >= 0.75 else "生气",
                        ),
                        PerformanceAction(
                            "PLAY_ANIMATION",
                            animation_id="stretch" if fatigue >= 0.75 else "short_pause",
                        ),
                    ),
                )

        beat = mainline.current_beat
        if (
            beat and beat.kind == "detour" and beat.return_to
            and beat_seconds >= settings.stream.director_detour_return_min_seconds
            and signals.silence_seconds >= settings.stream.director_quiet_seconds
        ):
            return self._act(
                mainline, activity, "RETURN_MAINLINE",
                (FactMutation("SET_MAINLINE_BEAT", target_beat_id=beat.return_to),),
                (PerformanceAction("SPEAK", template_family="RETURN_MAINLINE", emotion_hint="思考"),),
            )

        if activity_candidate and (
            settings.stream.director_activity_driver == "director"
            or settings.stream.director_mode in {"shadow", "ai_shadow"}
        ):
            return self._act(mainline, activity, str(activity_candidate["reason_code"]), (
                FactMutation(
                    "CHANGE_ACTIVITY", target_activity_id=activity_candidate["candidate"]["id"]
                ),
            ))

        if (
            settings.stream.director_performance_enabled
            and activity.category == "game"
            and signals.silence_seconds >= settings.stream.director_speak_cooldown_seconds
            and context.get("performance_allowed", False)
        ):
            return self._act(
                mainline, activity, "ROOM_QUIET", (),
                (PerformanceAction("SPEAK", template_family="QUIET_GAME_COMMENTARY", emotion_hint="思考"),),
            )
        return StreamerActionDecision.continue_(mainline, activity)

    @staticmethod
    def _act(
        mainline: StreamMainlineState,
        activity: StreamerActivityState,
        reason: str,
        facts: tuple[FactMutation, ...] = (),
        performance: tuple[PerformanceAction, ...] = (),
    ) -> StreamerActionDecision:
        return StreamerActionDecision(
            decision="ACT", reason_code=reason,
            stream_session_id=mainline.stream_session_id,
            plan_version=mainline.plan_version, beat_version=mainline.beat_version,
            activity_version=activity.version, fact_mutations=facts,
            performance_actions=performance,
        )


@dataclass(frozen=True)
class ActionExecutionResult:
    mainline: StreamMainlineState
    activity: StreamerActivityState
    committed_fact_types: tuple[str, ...] = ()


class StreamerActionExecutor:
    """事实 mutation 受控出口；Performance 不进入该事务。"""

    def __init__(
        self, mainline_service: StreamMainlineService,
        activity_service: StreamerActivityService,
    ):
        if mainline_service.database is not activity_service.database:
            raise ValueError("Mainline 与 Activity 必须使用同一数据库")
        self.mainline_service = mainline_service
        self.activity_service = activity_service
        self.database = mainline_service.database

    def execute(
        self, decision: StreamerActionDecision,
        mainline: StreamMainlineState, activity: StreamerActivityState,
        *, now: datetime | None = None,
    ) -> ActionExecutionResult | None:
        if not self._valid_base(decision, mainline, activity):
            return None
        if decision.decision == "CONTINUE":
            return ActionExecutionResult(mainline, activity)
        if len(decision.fact_mutations) > 2 or len(decision.performance_actions) > 2:
            return None
        fact_types = [item.type for item in decision.fact_mutations]
        if any(item not in FACT_TYPES for item in fact_types) or len(fact_types) != len(set(fact_types)):
            return None
        if any(
            (
                item.type == "SET_MAINLINE_BEAT"
                and (not item.target_beat_id or item.target_activity_id is not None)
            ) or (
                item.type == "CHANGE_ACTIVITY"
                and (not item.target_activity_id or item.target_beat_id is not None)
            )
            for item in decision.fact_mutations
        ):
            return None
        if any(item.type not in PERFORMANCE_TYPES for item in decision.performance_actions):
            return None
        if any(
            (
                item.type == "SPEAK"
                and (not item.template_family or item.animation_id is not None)
            ) or (
                item.type == "PLAY_ANIMATION"
                and (not item.animation_id or item.template_family is not None)
            )
            for item in decision.performance_actions
        ):
            return None
        if any(
            (item.type == "SPEAK" and item.template_family not in TEMPLATE_FAMILIES)
            or (item.type == "PLAY_ANIMATION" and item.animation_id not in ANIMATION_IDS)
            for item in decision.performance_actions
        ):
            return None
        if not decision.fact_mutations:
            return ActionExecutionResult(mainline, activity)

        beat_mutation = next((item for item in decision.fact_mutations if item.type == "SET_MAINLINE_BEAT"), None)
        activity_mutation = next((item for item in decision.fact_mutations if item.type == "CHANGE_ACTIVITY"), None)
        target_beat = mainline.plan.beat(beat_mutation.target_beat_id) if beat_mutation else None
        reference_now = now or datetime.now(timezone.utc)
        target_activity = (
            self.activity_service.validate_switch_target(
                current=activity,
                target_activity_id=activity_mutation.target_activity_id,
                theme_id=mainline.theme_id,
                now=reference_now,
                switch_cooldown_minutes=settings.stream.activity_switch_cooldown_minutes,
            )
            if activity_mutation else None
        )
        if beat_mutation and (not target_beat or target_beat.beat_id == mainline.current_beat_id):
            return None
        if activity_mutation and (
            not target_activity or target_activity["id"] == activity.activity_id
        ):
            return None
        if (
            target_beat and target_activity
            and target_activity["id"] not in target_beat.compatible_activity_ids
        ):
            return None
        changed_at = reference_now.isoformat()
        next_beat_version = mainline.beat_version + (1 if target_beat else 0)
        next_activity_version = activity.version + (1 if target_activity else 0)
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            mainline_row = conn.execute(
                "SELECT * FROM stream_mainline_sessions WHERE stream_session_id = ?",
                (mainline.stream_session_id,),
            ).fetchone()
            activity_row = conn.execute(
                "SELECT * FROM streamer_activity_sessions WHERE stream_session_id = ?",
                (activity.stream_session_id,),
            ).fetchone()
            if (
                not mainline_row or not activity_row
                or mainline_row["status"] != "active" or activity_row["ended_at"] is not None
                or mainline_row["plan_version"] != decision.plan_version
                or mainline_row["beat_version"] != decision.beat_version
                or activity_row["version"] != decision.activity_version
            ):
                return None
            if target_beat:
                conn.execute("""
                    UPDATE stream_mainline_sessions
                    SET current_beat_id = ?, current_beat_kind = ?, current_beat_label = ?,
                        beat_started_at = ?, beat_version = ?, trigger_source = ?, updated_at = ?
                    WHERE stream_session_id = ? AND beat_version = ? AND status = 'active'
                """, (
                    target_beat.beat_id, target_beat.kind, target_beat.label,
                    changed_at, next_beat_version, "stream_director", changed_at,
                    mainline.stream_session_id, mainline.beat_version,
                ))
                conn.execute("""
                    INSERT INTO stream_mainline_beat_transitions (
                        stream_session_id, beat_version, previous_beat_id,
                        beat_id, beat_kind, beat_label, activity_version,
                        trigger_source, reason_code, changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'stream_director', ?, ?)
                """, (
                    mainline.stream_session_id, next_beat_version, mainline.current_beat_id,
                    target_beat.beat_id, target_beat.kind, target_beat.label,
                    next_activity_version if target_activity else activity.version,
                    decision.reason_code, changed_at,
                ))
            if target_activity:
                conn.execute("""
                    UPDATE streamer_activity_sessions
                    SET activity_id = ?, category = ?, display_name = ?, object_name = ?,
                        started_at = ?, min_duration_minutes = ?, version = ?,
                        trigger_source = ?, public_performance = 0, updated_at = ?
                    WHERE stream_session_id = ? AND version = ? AND ended_at IS NULL
                """, (
                    target_activity["id"], target_activity["category"], target_activity["name"],
                    target_activity["object_name"], changed_at,
                    target_activity["min_duration_minutes"], next_activity_version,
                    decision.reason_code.casefold(), changed_at,
                    activity.stream_session_id, activity.version,
                ))
                conn.execute("""
                    INSERT INTO streamer_activity_transitions (
                        stream_session_id, version, previous_activity_id,
                        previous_display_name, previous_object_name, activity_id,
                        display_name, object_name, trigger_source,
                        public_performance, changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """, (
                    activity.stream_session_id, next_activity_version,
                    activity.activity_id, activity.display_name, activity.object_name,
                    target_activity["id"], target_activity["name"], target_activity["object_name"],
                    decision.reason_code.casefold(), changed_at,
                ))
            commit_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO stream_director_commits (
                    commit_id, stream_session_id, decision_source,
                    base_plan_version, base_beat_version, base_activity_version,
                    reason_code, fact_mutations_json, committed_beat_version,
                    committed_activity_version, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                commit_id, decision.stream_session_id, decision.decision_source,
                decision.plan_version, decision.beat_version, decision.activity_version,
                decision.reason_code,
                json.dumps([asdict(item) for item in decision.fact_mutations],
                           ensure_ascii=False, sort_keys=True),
                next_beat_version, next_activity_version, changed_at,
            ))
            next_mainline_row = conn.execute(
                "SELECT * FROM stream_mainline_sessions WHERE stream_session_id = ?",
                (mainline.stream_session_id,),
            ).fetchone()
            next_activity_row = conn.execute(
                "SELECT * FROM streamer_activity_sessions WHERE stream_session_id = ?",
                (activity.stream_session_id,),
            ).fetchone()
        return ActionExecutionResult(
            self.mainline_service._from_row(next_mainline_row),
            self.activity_service._from_row(next_activity_row),
            tuple(fact_types),
        )

    @staticmethod
    def _valid_base(
        decision: StreamerActionDecision,
        mainline: StreamMainlineState,
        activity: StreamerActivityState,
    ) -> bool:
        return (
            decision.schema_version == "streamer-action-decision-v1"
            and decision.decision in {"CONTINUE", "ACT"}
            and decision.stream_session_id == mainline.stream_session_id == activity.stream_session_id
            and decision.plan_version == mainline.plan_version
            and decision.beat_version == mainline.beat_version
            and decision.activity_version == activity.version
            and (decision.decision != "CONTINUE" or (
                not decision.fact_mutations and not decision.performance_actions
            ))
        )


class DirectorPerformanceTemplates:
    _TEMPLATES = {
        "GAME_FATIGUE": {
            "low": (("疲惫", "手都快打酸了，先歇一下……"), ("思考", "等下，我先缓缓。")),
            "high": (("生气", "先停一下，再打下去我要急了。"),),
        },
        "HIGH_STRESS": {
            "high": (("生气", "让我缓口气，等会儿再继续。"),),
        },
        "RETURN_MAINLINE": {
            "low": (("思考", "好像聊得有点远了，继续刚才的。"),
                    ("思考", "等下，我们是不是还有正事没做？")),
        },
        "QUIET_GAME_COMMENTARY": {
            "low": (("认真", "这里得认真一点了。"), ("思考", "嗯……这段有点难。")),
        },
    }

    def render(
        self, action: PerformanceAction, *, stress: float,
        mood: float = 0.5, darkness: float = 0.0,
        session_id: str, version: int,
    ) -> dict[str, Any] | None:
        if action.type != "SPEAK" or not action.template_family:
            return None
        variants = self._TEMPLATES.get(action.template_family, {})
        tense = stress >= 0.7 or darkness >= 0.7 or mood <= 0.25
        tier = "high" if tense and variants.get("high") else "low"
        choices = variants.get(tier) or variants.get("low")
        if not choices:
            return None
        digest = hashlib.sha256(
            f"{session_id}:{version}:{action.template_family}".encode("utf-8")
        ).digest()
        index = int.from_bytes(digest[:4], "big") % len(choices)
        emotion, text = choices[index]
        return {"emotions": [emotion], "sentences": [{"emotion": emotion, "text": text}]}


class StreamDirectorRuntime:
    """有界单消费者运行时；Shadow 是默认且不会改写事实。"""

    def __init__(
        self,
        *,
        context_provider: Callable[[], dict[str, Any] | None],
        executor: StreamerActionExecutor,
        on_committed: Callable[[ActionExecutionResult, StreamerActionDecision], Awaitable[None]],
        on_performance: Callable[[PerformanceAction, dict[str, Any]], Awaitable[bool]],
        policy: DeterministicStreamDirector | None = None,
        signal_tracker: DirectorSignalTracker | None = None,
        ai_candidate: Any = None,
    ):
        self.context_provider = context_provider
        self.executor = executor
        self.on_committed = on_committed
        self.on_performance = on_performance
        self.policy = policy or DeterministicStreamDirector()
        self.signals = signal_tracker or DirectorSignalTracker()
        self.ai_candidate = ai_candidate
        self._normal_queue_capacity = settings.stream.director_queue_capacity
        # 为开播/恢复/Activity transition/SC 完成保留少量槽位；总容量仍有界。
        self._critical_queue_reserve = 4
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(
            self._normal_queue_capacity + self._critical_queue_reserve
        )
        self._running = False
        self._worker: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None
        self._sequence = 0
        self._last_trigger: dict[str, float] = {}
        self._last_fact_at = 0.0
        self._last_public_at = 0.0
        self._last_speak_at = 0.0
        self._last_ai_at = 0.0
        self._started_at = time.monotonic()
        self._room_band = "normal"
        self._persona_bands: dict[str, bool] = {}
        self._shadow_seen: set[tuple[Any, ...]] = set()
        self._session_counts: dict[str, dict[str, int]] = {}
        self._active_session_id: str | None = None
        self._stats = {
            "triggers": 0, "coalesced": 0, "queue_dropped": 0,
            "evaluations": 0, "continues": 0, "shadow_actions": 0,
            "fact_commits": 0, "performances": 0, "busy_skips": 0,
            "ai_calls": 0, "ai_continues": 0, "ai_failures": 0,
            "legacy_activity_proposals": 0, "legacy_activity_commits": 0,
        }
        self._last_legacy_activity_observation: dict[str, Any] | None = None

    async def start(self) -> None:
        if self._running or not settings.stream.director_enabled:
            return
        self._running = True
        self._started_at = time.monotonic()
        self.signals.record_activity()
        self._worker = asyncio.create_task(self._worker_loop())
        self._ticker = asyncio.create_task(self._tick_loop())
        await self.notify("recovery", priority=1)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in (self._ticker, self._worker):
            if task:
                task.cancel()
        for task in (self._ticker, self._worker):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ticker = self._worker = None

    async def notify(self, family: str, *, priority: int = 5) -> bool:
        if not self._running:
            return False
        now = time.monotonic()
        if now - self._last_trigger.get(family, -1e9) < settings.stream.director_trigger_coalesce_seconds:
            self._stats["coalesced"] += 1
            return False
        self._last_trigger[family] = now
        self._sequence += 1
        if self._queue.qsize() >= self._normal_queue_capacity and priority > 3:
            self._stats["queue_dropped"] += 1
            return False
        try:
            self._queue.put_nowait((priority, self._sequence, family))
            self._stats["triggers"] += 1
            return True
        except asyncio.QueueFull:
            self._stats["queue_dropped"] += 1
            return False

    def observe_persona_event(self, event: Any, snapshot: dict | None = None) -> None:
        self.signals.observe_persona_event(event, snapshot)
        if not self._running:
            return
        name = event.__class__.__name__
        if name in {"StreamLifecycleEvent", "GiftReceivedEvent", "ModerationActionEvent"}:
            asyncio.create_task(self.notify(name, priority=2))
            return

        # 单条弹幕和每个 atmosphere tick 只更新窗口；只有跨入持续安静或
        # 明显活跃区间才触发评估。这样事件驱动不等价于“每条消息都跑策略”。
        room = self.signals.snapshot(getattr(event, "occurred_at", None))
        if name in {"DanmakuReceivedEvent", "AudienceAtmosphereTickEvent"}:
            if room.rate_60s >= 5 and self._room_band != "active":
                self._room_band = "active"
                asyncio.create_task(self.notify("room_surge", priority=6))
            elif room.rate_60s < 3 and self._room_band == "active":
                self._room_band = "normal"
        elif name == "SilenceTickEvent":
            if (
                room.silence_seconds >= settings.stream.director_quiet_seconds
                and self._room_band != "quiet"
            ):
                self._room_band = "quiet"
                asyncio.create_task(self.notify("room_quiet", priority=6))
            elif room.silence_seconds < settings.stream.director_quiet_seconds * 0.75:
                self._room_band = "normal"

        after = (snapshot or {}).get("state_after", {})
        persona = after.get("persona", {}) if isinstance(after, dict) else {}
        internal = after.get("internal", {}) if isinstance(after, dict) else {}
        thresholds = {
            "stress": (persona.get("stress"), 0.8),
            "darkness": (persona.get("darkness"), 0.75),
            "fatigue": (internal.get("fatigue"), 0.75),
            "arousal": (internal.get("arousal"), 0.8),
        }
        crossed = False
        for metric, (raw_value, threshold) in thresholds.items():
            if raw_value is None:
                continue
            value = float(raw_value)
            was_high = self._persona_bands.get(metric, False)
            is_high = value >= threshold if not was_high else value >= threshold - 0.1
            self._persona_bands[metric] = is_high
            crossed = crossed or (is_high and not was_high)
        if crossed:
            asyncio.create_task(self.notify("persona_threshold", priority=4))

    def observe_legacy_activity_proposal(
        self, *, base_activity_version: int, proposal: dict[str, Any]
    ) -> None:
        """Shadow 对照只保留聚合与最近结构化结果，不写 proposal 历史表。"""
        self._stats["legacy_activity_proposals"] += 1
        self._last_legacy_activity_observation = {
            "base_activity_version": base_activity_version,
            "target_activity_id": proposal["candidate"]["id"],
            "reason_code": proposal["reason_code"],
            "committed": False,
        }

    def observe_legacy_activity_commit(self, changed: StreamerActivityState) -> None:
        self._stats["legacy_activity_commits"] += 1
        if self._last_legacy_activity_observation:
            self._last_legacy_activity_observation = {
                **self._last_legacy_activity_observation,
                "committed": True,
                "committed_activity_id": changed.activity_id,
                "committed_version": changed.version,
                "matched": (
                    self._last_legacy_activity_observation["target_activity_id"]
                    == changed.activity_id
                ),
            }

    def get_stats(self) -> dict[str, Any]:
        evaluated = max(1, self._stats["evaluations"])
        return {
            **self._stats,
            "running": self._running,
            "mode": settings.stream.director_mode,
            "activity_driver": settings.stream.director_activity_driver,
            "ai_rollout_percent": settings.stream.director_ai_rollout_percent,
            "queue_size": self._queue.qsize(),
            "continue_ratio": self._stats["continues"] / evaluated,
            "signals": asdict(self.signals.snapshot()),
            "last_legacy_activity_observation": self._last_legacy_activity_observation,
        }

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(settings.stream.director_tick_seconds)
                await self.notify("fallback_tick", priority=9)
            except asyncio.CancelledError:
                break

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                _, _, family = await self._queue.get()
                try:
                    await self._evaluate(family)
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Stream Director 评估失败，安全继续当前状态: %s", exc)

    async def _evaluate(self, family: str) -> None:
        context = self.context_provider()
        if not context or not context.get("is_live"):
            return
        session_id = context["mainline"].stream_session_id
        if session_id != self._active_session_id:
            self._active_session_id = session_id
            self._last_fact_at = 0.0
            self._last_public_at = 0.0
            self._last_speak_at = 0.0
            self._last_ai_at = 0.0
            self._started_at = time.monotonic()
            self._shadow_seen.clear()
        if context.get("busy"):
            self._stats["busy_skips"] += 1
            return
        self._stats["evaluations"] += 1
        context["performance_allowed"] = self._performance_allowed(context)
        signal_snapshot = self.signals.snapshot(context.get("now"))
        decision = self.policy.evaluate(context, signal_snapshot)
        if (
            decision.decision == "CONTINUE"
            and family != "recovery"
            and settings.stream.director_mode in {"ai_shadow", "ai"}
            and self.ai_candidate is not None
            and self._ai_allowed(context, signal_snapshot)
        ):
            self._stats["ai_calls"] += 1
            self._last_ai_at = time.monotonic()
            candidate = await self.ai_candidate.decide(context, signal_snapshot)
            latest = self.context_provider()
            if not candidate or not latest or latest.get("busy"):
                self._stats["ai_failures"] += 1
                return
            if candidate.decision == "CONTINUE":
                self._stats["ai_continues"] += 1
                self._stats["continues"] += 1
                return
            context = latest
            decision = candidate
        if decision.decision == "CONTINUE":
            self._stats["continues"] += 1
            return
        if settings.stream.director_mode in {"shadow", "ai_shadow"}:
            signature = (
                decision.stream_session_id, decision.beat_version,
                decision.activity_version, decision.reason_code,
                tuple((item.type, item.target_beat_id, item.target_activity_id)
                      for item in decision.fact_mutations),
                tuple((item.type, item.template_family, item.animation_id)
                      for item in decision.performance_actions),
            )
            if signature in self._shadow_seen:
                self._stats["continues"] += 1
                return
            if len(self._shadow_seen) >= 128:
                self._shadow_seen.clear()
            self._shadow_seen.add(signature)
            self._stats["shadow_actions"] += 1
            logger.debug("Director shadow candidate: family=%s reason=%s", family, decision.reason_code)
            return
        now_mono = time.monotonic()
        if decision.fact_mutations and now_mono - self._last_fact_at < settings.stream.director_fact_cooldown_seconds:
            self._stats["continues"] += 1
            return
        result = self.executor.execute(
            decision, context["mainline"], context["activity"], now=context.get("now")
        )
        if not result:
            return
        if result.committed_fact_types:
            self._last_fact_at = now_mono
            self._stats["fact_commits"] += 1
            await self.on_committed(result, decision)
        if decision.performance_actions and self._performance_allowed(context):
            for action in decision.performance_actions:
                if action.type == "SPEAK" and context.get("speak_suppressed"):
                    continue
                performed = await self.on_performance(action, context)
                if not performed:
                    continue
                self._stats["performances"] += 1
                self._last_public_at = now_mono
                if action.type == "SPEAK":
                    self._last_speak_at = now_mono
                self._increment_public(context["mainline"].stream_session_id, action.type)

    def _performance_allowed(self, context: dict[str, Any]) -> bool:
        if not settings.stream.director_performance_enabled or context.get("viewer_count", 0) <= 0:
            return False
        now = time.monotonic()
        if now - self._started_at < settings.stream.director_public_action_cooldown_seconds:
            return False
        if context.get("busy") or now - self._last_public_at < settings.stream.director_public_action_cooldown_seconds:
            return False
        counts = self._session_counts.get(context["mainline"].stream_session_id, {})
        if counts.get("public", 0) >= settings.stream.director_max_public_actions_per_stream:
            return False
        if (
            counts.get("SPEAK", 0) >= settings.stream.director_max_speaks_per_stream
            or now - self._last_speak_at < settings.stream.director_speak_cooldown_seconds
        ):
            context["speak_suppressed"] = True
        return True

    def _increment_public(self, session_id: str, action_type: str) -> None:
        counts = self._session_counts.setdefault(session_id, {})
        counts["public"] = counts.get("public", 0) + 1
        counts[action_type] = counts.get(action_type, 0) + 1

    def _ai_allowed(
        self, context: dict[str, Any], signals: DirectorSignalSnapshot
    ) -> bool:
        session_id = context["mainline"].stream_session_id
        rollout = settings.stream.director_ai_rollout_percent
        if rollout <= 0:
            return False
        bucket = int.from_bytes(
            hashlib.sha256(f"stream-director-rollout:{session_id}".encode()).digest()[:4],
            "big",
        ) % 100
        if bucket >= rollout:
            return False
        counts = self._session_counts.get(session_id, {})
        if counts.get("AI", 0) >= settings.stream.director_ai_max_per_stream:
            return False
        if time.monotonic() - self._last_ai_at < settings.stream.director_ai_min_interval_seconds:
            return False
        # AI 只处理少数模糊情形，普通 Tick 不足以触发。
        ambiguous = (
            context["mainline"].current_beat_kind == "detour"
            or signals.silence_seconds >= settings.stream.director_quiet_seconds
            or (signals.sentiment_samples >= 5 and abs(signals.audience_sentiment) >= 0.4)
        )
        if ambiguous:
            counts = self._session_counts.setdefault(session_id, {})
            counts["AI"] = counts.get("AI", 0) + 1
        return ambiguous
