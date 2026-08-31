"""P21 场次级公共事实与可恢复总结任务。

本模块只处理脱敏、聚合后的场次事实；不读取原始弹幕、SC 正文、
账号身份或个人记忆。外部 AI 消费器将在独立阶段接入，创建任务本身
不会新增模型调用。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from config import settings
from kangel.infrastructure.database import DatabaseManager, db_manager
from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
from kangel.integrations.ai.service import AIService, ai_service
from kangel.shared.logging import logger


FACTS_VERSION = "stream_session_facts_v1"
_MAX_ACTIVITY_ITEMS = 12
_ROOM_ATMOSPHERES = frozenset({"calm", "active", "mixed", "tense"})


def _clip_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bounded_float(value: object, low: float = -1.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return 0.0


class StreamSessionSummaryService:
    """以排期场次为唯一事实源，持久化可审计的公共总结输入。"""

    def __init__(self, database: DatabaseManager):
        self.database = database
        self._stats = {
            "session_opened": 0,
            "snapshot_frozen": 0,
            "snapshot_recovered": 0,
            "snapshot_skipped": 0,
            "summary_injection_hit": 0,
            "retention_tasks_deleted": 0,
            "retention_facts_deleted": 0,
        }
        self._opened_session_ids: set[str] = set()
        self._last_cleanup_at = 0.0

    def open_session(
        self,
        *,
        stream_session_id: str,
        scheduled_start_at: str,
        scheduled_end_at: str,
        schedule_timezone: str,
        theme: Optional[dict],
        persona_state: Optional[dict],
        opened_at: Optional[datetime] = None,
    ) -> bool:
        """仅在排期确认开播时创建事实；重启后复用同一场次。"""
        now = opened_at or datetime.now(timezone.utc)
        facts = {
            "schema_version": FACTS_VERSION,
            "session": {
                "stream_session_id": _clip_text(stream_session_id, 128),
                "scheduled_start_at": _clip_text(scheduled_start_at, 64),
                "scheduled_end_at": _clip_text(scheduled_end_at, 64),
                "schedule_timezone": _clip_text(schedule_timezone, 64),
            },
            "theme": self._safe_theme(theme),
            "persona": {"start": self._safe_persona(persona_state)},
            "activity_timeline": [],
            "room": {},
            "public_interaction": {},
        }
        created = self.database.create_stream_session_facts(
            stream_session_id=stream_session_id,
            scheduled_start_at=scheduled_start_at,
            scheduled_end_at=scheduled_end_at,
            schedule_timezone=schedule_timezone,
            facts=facts,
            source_version=FACTS_VERSION,
            created_at=now.astimezone(timezone.utc).isoformat(),
        )
        if created:
            self._opened_session_ids.add(stream_session_id)
            self._stats["session_opened"] += 1
            logger.info("P21 场次事实已创建")
        return created

    def reconcile_closed_sessions(
        self,
        *,
        now: Optional[datetime] = None,
        closure_context: Callable[[str], dict[str, Any]],
    ) -> list[str]:
        """冻结已过排期结束点的事实，覆盖正常下播与宕机恢复。"""
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        frozen = []
        for row in self.database.list_active_stream_session_facts():
            try:
                scheduled_end = datetime.fromisoformat(row["scheduled_end_at"])
                if scheduled_end.tzinfo is None:
                    scheduled_end = scheduled_end.replace(tzinfo=timezone.utc)
                if scheduled_end.astimezone(timezone.utc) > reference:
                    continue
            except (TypeError, ValueError):
                logger.warning("P21 跳过无效场次结束时间")
                self._stats["snapshot_skipped"] += 1
                continue

            facts = self._freeze_facts(
                row["facts"], closure_context(row["stream_session_id"])
            )
            if self.database.freeze_stream_session_and_enqueue_summary(
                stream_session_id=row["stream_session_id"],
                facts=facts,
                source_version=FACTS_VERSION,
                frozen_at=reference.isoformat(),
            ):
                frozen.append(row["stream_session_id"])
                self._stats["snapshot_frozen"] += 1
                if row["stream_session_id"] not in self._opened_session_ids:
                    self._stats["snapshot_recovered"] += 1
                logger.info("P21 场次快照已冻结并入队")
        self._maybe_purge_expired(reference)
        return frozen

    def get_stats(self) -> dict[str, Any]:
        return {**self._stats, **self.database.get_stream_session_summary_stats()}

    def _maybe_purge_expired(self, now: datetime) -> None:
        """每小时最多清理一次；pending/processing 任务永不受保留期影响。"""
        monotonic_now = time.monotonic()
        if monotonic_now - self._last_cleanup_at < 3600:
            return
        self._last_cleanup_at = monotonic_now
        result = self.database.purge_expired_stream_session_summaries(
            (now - timedelta(days=settings.session_summary.retention_days)).isoformat()
        )
        self._stats["retention_tasks_deleted"] += result["tasks"]
        self._stats["retention_facts_deleted"] += result["facts"]

    def build_reply_context(
        self, *, current_stream_session_id: str, message: str,
        current_activity: Optional[dict], prompt_chars: int,
    ) -> Optional[dict]:
        """仅在当前已验证活动与上一场事实连续时提供极短、低权重背景。"""
        previous = self.database.get_latest_completed_stream_session_summary(
            exclude_stream_session_id=current_stream_session_id
        )
        if not previous:
            return None
        summary = previous.get("summary") or {}
        current_activity_id = str((current_activity or {}).get("activity_id", ""))
        known_ids = {
            str(item.get("activity_id", ""))
            for item in summary.get("activity_timeline", []) if isinstance(item, dict)
        }
        # 当前活动事实连续是唯一允许自动命中的条件；不凭关键词猜测个人或公共话题。
        if not current_activity_id or current_activity_id not in known_ids:
            return None
        if not self._message_references_current_activity(message, current_activity):
            return None
        text = _clip_text(summary.get("session_summary"), prompt_chars)
        if not text:
            return None
        self._stats["summary_injection_hit"] += 1
        return {
            "session_summary": text,
            "mood_arc": _clip_text(summary.get("mood_arc"), min(120, prompt_chars)),
            "activity_id": current_activity_id,
            "source": "previous_completed_stream",
            "message_related": True,
        }

    def get_activity_initialization_hint(
        self, *, current_stream_session_id: str
    ) -> Optional[str]:
        """返回上一场已完成总结中的最后一个活动 ID，供新场作受限候选。"""
        previous = self.database.get_latest_completed_stream_session_summary(
            exclude_stream_session_id=current_stream_session_id
        )
        if not previous:
            return None
        timeline = (previous.get("summary") or {}).get("activity_timeline") or []
        for item in reversed(timeline):
            if isinstance(item, dict):
                activity_id = _clip_text(item.get("activity_id"), 64)
                if activity_id:
                    return activity_id
        return None

    @staticmethod
    def _message_references_current_activity(
        message: str, current_activity: Optional[dict]
    ) -> bool:
        """只用服务端活动事实做字面命中，不能猜测观众身份或话题。"""
        normalized_message = "".join(
            char for char in str(message or "").casefold() if char.isalnum()
        )
        if not normalized_message:
            return False
        activity = current_activity or {}
        for key in ("activity_id", "category", "display_name", "object_name"):
            candidate = "".join(
                char for char in str(activity.get(key, "")).casefold() if char.isalnum()
            )
            # 单字符类别（例如中文“聊”）过宽，不能作为跨场注入依据。
            if len(candidate) >= 2 and candidate in normalized_message:
                return True
        return False

    @staticmethod
    def _safe_persona(state: Optional[dict]) -> dict[str, float]:
        value = state or {}
        return {
            "mood": _bounded_float(value.get("mood", .5), 0, 1),
            "stress": _bounded_float(value.get("stress", .3), 0, 1),
            "darkness": _bounded_float(value.get("darkness", .2), 0, 1),
        }

    @staticmethod
    def _safe_theme(theme: Optional[dict]) -> dict[str, Any]:
        value = theme or {}
        special = value.get("special_date_theme")
        return {
            "daily_theme_id": _clip_text(value.get("id"), 64),
            "daily_theme_name": _clip_text(value.get("name"), 80),
            "date": _clip_text(value.get("date"), 16),
            "special_date_theme": (
                {
                    "id": _clip_text(special.get("id"), 64),
                    "name": _clip_text(special.get("name"), 80),
                    "title": _clip_text(special.get("title"), 120),
                }
                if isinstance(special, dict) else None
            ),
        }

    def _freeze_facts(self, opened_facts: dict, context: dict[str, Any]) -> dict:
        facts = dict(opened_facts)
        activities = []
        for item in (context.get("activity_timeline") or [])[:_MAX_ACTIVITY_ITEMS]:
            if not isinstance(item, dict):
                continue
            try:
                version = max(1, int(item.get("version", 1) or 1))
            except (TypeError, ValueError):
                version = 1
            activities.append({
                "version": version,
                "activity_id": _clip_text(item.get("activity_id"), 64),
                "category": _clip_text(item.get("category"), 64),
                "display_name": _clip_text(item.get("display_name"), 80),
                "object_name": _clip_text(item.get("object_name"), 100),
                "changed_at": _clip_text(item.get("changed_at"), 64),
                "trigger_source": _clip_text(item.get("trigger_source"), 64),
            })
        facts["activity_timeline"] = activities
        facts["persona"] = {
            "start": self._safe_persona((opened_facts.get("persona") or {}).get("start")),
            "end": self._safe_persona(context.get("persona_state")),
        }
        facts["room"] = {
            "viewer_count_at_close": max(0, int(context.get("viewer_count", 0) or 0)),
            "danmaku_rate_at_close": max(0, int(context.get("danmaku_rate", 0) or 0)),
            "audience_sentiment_at_close": _bounded_float(
                context.get("audience_sentiment", 0.0)
            ),
            "audience_sample_count": max(
                0, int(context.get("audience_sample_count", 0) or 0)
            ),
        }
        # 仅允许类别聚合；没有可靠公共话题时保持空数组，禁止模型补造事实。
        facts["public_interaction"] = {
            "categories": [],
            "open_public_threads": [],
        }
        return facts


class SessionSummaryValidationError(ValueError):
    """外部模型输出不满足公共事实白名单。"""


class SessionSummaryValidator:
    """严格限制总结只能复述已冻结的场次事实。

    模型只能提出受白名单约束的结构选择；任何自由文本都会在入库前由
    冻结事实重新生成。这样即使模型写出观众、关系或未发生的情节，也不
    会成为可读取的场次总结。
    """

    @classmethod
    def validate(cls, response: object, facts: dict) -> dict:
        payload = cls._parse(response)
        required = {
            "session_summary", "mood_arc", "room_atmosphere",
            "activity_timeline", "open_public_threads",
            "next_stream_callbacks", "evidence_version",
        }
        if set(payload) != required:
            raise SessionSummaryValidationError("summary_fields")
        if payload["evidence_version"] != FACTS_VERSION:
            raise SessionSummaryValidationError("evidence_version")

        # 仍校验模型遵守既定 JSON/字符契约，但不得把自由文本直接持久化。
        cls._text(payload["session_summary"], 480, "session_summary")
        cls._text(payload["mood_arc"], 160, "mood_arc")
        model_atmosphere = str(payload["room_atmosphere"] or "").strip().casefold()
        if model_atmosphere not in _ROOM_ATMOSPHERES:
            raise SessionSummaryValidationError("room_atmosphere")

        known_activities = {
            (str(item.get("activity_id", "")), int(item.get("version", 0) or 0))
            for item in facts.get("activity_timeline", []) if isinstance(item, dict)
        }
        activity_timeline = cls._activities(payload["activity_timeline"], known_activities)
        allowed_threads = {
            _clip_text(item, 120)
            for item in (facts.get("public_interaction") or {}).get(
                "open_public_threads", []
            )
        }
        open_threads = cls._allowed_threads(
            payload["open_public_threads"], allowed_threads, "open_public_threads"
        )
        callbacks = cls._allowed_threads(
            payload["next_stream_callbacks"], allowed_threads, "next_stream_callbacks"
        )
        atmosphere = cls._canonical_room_atmosphere(facts)
        canonical_activities = cls._canonical_activities(activity_timeline, facts)
        return {
            "session_summary": cls._canonical_session_summary(
                facts, canonical_activities, atmosphere
            ),
            "mood_arc": cls._canonical_mood_arc(facts),
            "room_atmosphere": atmosphere,
            "activity_timeline": canonical_activities,
            "open_public_threads": open_threads,
            "next_stream_callbacks": callbacks,
            "evidence_version": FACTS_VERSION,
        }

    @staticmethod
    def _canonical_room_atmosphere(facts: dict) -> str:
        """从冻结的聚合数值得出有限枚举，不能由模型自由判断。"""
        room = facts.get("room") or {}
        samples = max(0, int(room.get("audience_sample_count", 0) or 0))
        sentiment = _bounded_float(room.get("audience_sentiment_at_close", 0.0))
        danmaku_rate = max(0, int(room.get("danmaku_rate_at_close", 0) or 0))
        if samples <= 0:
            return "mixed"
        if sentiment <= -0.35:
            return "tense"
        if sentiment >= 0.35 or danmaku_rate >= 40:
            return "active"
        if danmaku_rate <= 1 and abs(sentiment) < 0.15:
            return "calm"
        return "mixed"

    @classmethod
    def _canonical_activities(cls, selected: list[dict], facts: dict) -> list[dict]:
        """保留模型选择的活动键，但用冻结活动对象重新生成说明。"""
        facts_by_key = {
            (str(item.get("activity_id", "")), int(item.get("version", 0) or 0)): item
            for item in facts.get("activity_timeline", []) if isinstance(item, dict)
        }
        result = []
        for item in selected:
            key = (item["activity_id"], item["version"])
            fact = facts_by_key[key]
            display_name = _clip_text(fact.get("display_name"), 80)
            object_name = _clip_text(fact.get("object_name"), 100)
            summary = "：".join(part for part in (display_name, object_name) if part)
            result.append({
                "activity_id": key[0],
                "version": key[1],
                "summary": summary or "已验证活动",
            })
        return result

    @classmethod
    def _canonical_session_summary(
        cls, facts: dict, activities: list[dict], atmosphere: str
    ) -> str:
        """所有可读取叙述均由已冻结主题、活动和聚合氛围组成。"""
        theme = facts.get("theme") or {}
        theme_name = _clip_text(theme.get("daily_theme_name"), 80)
        special = theme.get("special_date_theme") or {}
        special_title = _clip_text(special.get("title"), 120)
        labels = [_clip_text(item.get("summary"), 120) for item in activities]
        labels = [label for label in labels if label]
        atmosphere_text = {
            "calm": "平稳", "active": "活跃", "mixed": "混合", "tense": "紧张",
        }[atmosphere]
        lead = "本场按排期进行"
        if theme_name:
            lead += f"，主题为“{theme_name}”"
        if special_title:
            lead += f"，特殊日期主题为“{special_title}”"
        if labels:
            lead += "，已验证活动包括“" + "”、“".join(labels) + "”"
        return _clip_text(f"{lead}。房间聚合氛围为{atmosphere_text}。", 480)

    @staticmethod
    def _canonical_mood_arc(facts: dict) -> str:
        """人格走向仅来自冻结的起止三轴，不读取模型的心理叙述。"""
        persona = facts.get("persona") or {}
        start, end = persona.get("start") or {}, persona.get("end") or {}

        def direction(key: str, label: str) -> str:
            delta = _bounded_float(end.get(key, 0.0), 0.0, 1.0) - _bounded_float(
                start.get(key, 0.0), 0.0, 1.0
            )
            if delta > 0.04:
                return f"{label}上升"
            if delta < -0.04:
                return f"{label}下降"
            return f"{label}基本平稳"

        return "，".join((
            direction("mood", "心情"),
            direction("stress", "压力"),
            direction("darkness", "阴暗度"),
        )) + "。"

    @staticmethod
    def _parse(response: object) -> dict:
        text = str(response or "").strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise SessionSummaryValidationError("summary_json")
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise SessionSummaryValidationError("summary_json") from exc
        if not isinstance(value, dict):
            raise SessionSummaryValidationError("summary_json")
        return value

    @staticmethod
    def _text(value: object, limit: int, field: str) -> str:
        text = _clip_text(value, limit + 1)
        if not text or len(text) > limit:
            raise SessionSummaryValidationError(field)
        return text

    @classmethod
    def _activities(cls, value: object, known: set[tuple[str, int]]) -> list[dict]:
        if not isinstance(value, list) or len(value) > _MAX_ACTIVITY_ITEMS:
            raise SessionSummaryValidationError("activity_timeline")
        result, seen = [], set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"activity_id", "version", "summary"}:
                raise SessionSummaryValidationError("activity_timeline")
            try:
                key = (str(item["activity_id"]), int(item["version"]))
            except (TypeError, ValueError):
                raise SessionSummaryValidationError("activity_timeline") from None
            if key not in known or key in seen:
                raise SessionSummaryValidationError("activity_timeline")
            result.append({
                "activity_id": key[0], "version": key[1],
                "summary": cls._text(item["summary"], 120, "activity_timeline"),
            })
            seen.add(key)
        return result

    @classmethod
    def _allowed_threads(cls, value: object, allowed: set[str], field: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 3:
            raise SessionSummaryValidationError(field)
        result = [cls._text(item, 120, field) for item in value]
        if len(set(result)) != len(result) or any(item not in allowed for item in result):
            raise SessionSummaryValidationError(field)
        return result


class StreamSessionSummaryConsumer:
    """独立、低优先级的总结消费者；不会占用普通回复或 SC 的并发闸门。"""

    def __init__(self, database: DatabaseManager, ai_client: AIService = ai_service):
        self.database = database
        self.ai_client = ai_client
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stats = {
            "claimed": 0, "completed": 0, "failed": 0, "retried": 0,
            "deferred": 0, "disabled": 0,
        }

    async def start(self) -> None:
        if self._running:
            return
        if not settings.session_summary.ai_enabled:
            self._stats["disabled"] += 1
            logger.info("P21 场次总结 AI 消费器已按配置禁用")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="stream-session-summary")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                processed = await self.process_once()
                if not processed:
                    await asyncio.sleep(settings.session_summary.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("P21 场次总结消费循环异常")
                await asyncio.sleep(settings.session_summary.poll_interval_seconds)

    async def process_once(self) -> bool:
        if not settings.session_summary.ai_enabled:
            return False
        # SC 或普通回复正在占用/等待时，低优先级总结不领取任务。
        from kangel.integrations.superchat.service import sc_service
        gate = ai_reply_work_gate.snapshot()
        if gate["active"] or gate["waiting"] or await asyncio.to_thread(sc_service.has_active_work):
            self._stats["deferred"] += 1
            return False
        now = datetime.now(timezone.utc).isoformat()
        item = await asyncio.to_thread(
            self.database.claim_next_stream_session_summary_task,
            lease_seconds=settings.session_summary.processing_lease_seconds,
            now=now,
        )
        if not item:
            return False
        self._stats["claimed"] += 1
        logger.info("P21 场次总结任务已领取")
        try:
            response = await asyncio.wait_for(
                self.ai_client.run(
                    messages=self._messages(item["input_facts"]),
                    role="default",
                    model=settings.ai.default_model,
                    model_mode="role_hint",
                    temperature=0.0,
                    response_format={"type": "object"},
                    timeout=settings.session_summary.ai_timeout_seconds,
                ),
                timeout=settings.session_summary.ai_timeout_seconds,
            )
            summary = SessionSummaryValidator.validate(
                response.get("reply", ""), item["input_facts"]
            )
            completed = await asyncio.to_thread(
                self.database.complete_stream_session_summary_task,
                stream_session_id=item["stream_session_id"], summary=summary,
                now=datetime.now(timezone.utc).isoformat(),
            )
            if completed:
                self._stats["completed"] += 1
                logger.info("P21 场次总结任务已完成")
            return True
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.database.release_stream_session_summary_task,
                item["stream_session_id"], datetime.now(timezone.utc).isoformat(),
            )
            raise
        except SessionSummaryValidationError as exc:
            await self._fail(item["stream_session_id"], str(exc) or "invalid_summary")
            return True
        except Exception:
            await self._fail(item["stream_session_id"], "summary_generation_failed")
            return True

    async def _fail(self, stream_session_id: str, error_code: str) -> None:
        status = await asyncio.to_thread(
            self.database.fail_stream_session_summary_task,
            stream_session_id=stream_session_id, error_code=error_code,
            max_attempts=settings.session_summary.max_attempts,
            now=datetime.now(timezone.utc).isoformat(),
        )
        if status == "failed":
            self._stats["failed"] += 1
            logger.warning("P21 场次总结任务已失败: code=%s", error_code)
        elif status == "pending":
            self._stats["retried"] += 1
            logger.warning("P21 场次总结任务将重试: code=%s", error_code)

    @staticmethod
    def _messages(facts: dict) -> list[dict]:
        return [
            {
                "role": "system",
                "content": (
                    "你是直播场次公共事实总结器。只能使用用户消息中提供的已冻结事实；"
                    "不得编造活动、观众、关系、对话、未完话题或心理推理。"
                    "不得输出原始弹幕、SC、账号、昵称、IP、令牌或隐藏思维链。只输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "已冻结的最小必要场次事实：\n"
                    + json.dumps(facts, ensure_ascii=False, sort_keys=True)
                    + "\n\n返回且只能返回这些字段："
                    "session_summary(不超过480字)、mood_arc(不超过160字)、"
                    "room_atmosphere(calm|active|mixed|tense)、"
                    "activity_timeline([{activity_id,version,summary}])、"
                    "open_public_threads、next_stream_callbacks、"
                    f"evidence_version(必须为 {FACTS_VERSION})。"
                    "活动只能引用输入中的 activity_id+version；活动 summary 只能简述对应输入活动，"
                    "不得出现观众、关系、私下经历或未发生的内容。输入没有已验证公开话题时，"
                    "open_public_threads 与 next_stream_callbacks 必须为空数组。"
                ),
            },
        ]

    def get_stats(self) -> dict[str, int]:
        return dict(self._stats)


__all__ = [
    "FACTS_VERSION", "SessionSummaryValidationError", "SessionSummaryValidator",
    "StreamSessionSummaryService", "StreamSessionSummaryConsumer",
    "stream_session_summary_consumer",
]


stream_session_summary_consumer = StreamSessionSummaryConsumer(db_manager)
