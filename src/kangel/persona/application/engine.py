import asyncio
from time import perf_counter
from functools import wraps
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from uuid import uuid4
from ..domain.state import (
    PersonaState, PersonaBehavior, PersonaDecision, EmotionDelta,
    InternalPersonaState, InternalStateDelta
)
from config import settings
from kangel.integrations.ai.service import ai_service
from kangel.integrations.ai.prompts import (
    persona_catalog,
    persona_decision_prompt_builder,
    persona_qa_selector,
    streamer_reply_prompt_builder,
)
from kangel.integrations.ai.persona import (
    PersonaEvidenceSelector,
    build_style_vector,
    persona_prompt_metrics,
    resolve_prompt_mode,
)
from kangel.shared.logging import logger
from .impact_analyzer import persona_impact_analyzer
from ..domain.dynamics import DynamicsContext, PersonaAffectAnchor
from kangel.audience.application.relationship_service import audience_relationship_manager
from ..domain.events import SemanticImpactAnalyzedEvent
from ..domain.mutations import PersonaMutation
from .runtime import internal_state_dynamics, persona_dynamics, persona_event_pipeline
from kangel.danmaku.application.memory import danmaku_memory_manager
from kangel.infrastructure.database import db_manager
from kangel.infrastructure.reply_timing import reply_timing_metrics
from kangel.infrastructure.timing_trace import mark_current, note_current
from kangel.danmaku.application.pool import danmaku_pool
from .emotion_manager import emotion_manager
from .intent_state import StreamerIntentStateService
from .response_planner import response_planner
from .intent_shadow import intent_candidate_shadow_service
from .prompt_ram import prompt_ram_service
from kangel.audience.application.nickname_history import nickname_history_context_manager
from kangel.danmaku.application.language import (
    english_surprise_joke_service,
    language_detector,
    reply_language_policy,
)
from kangel.memory.application.long_term_memory import ConversationContinuityAnalyzer, long_term_memory_manager
from kangel.memory.application.episodic import episodic_memory_manager
from kangel.stream.application.metadata import stream_metadata_pusher
from ..infrastructure.state_repository import (
    DatabasePersonaStateRepository,
    PersonaStateRepository,
)


_damaku_lock = asyncio.Lock()
_conversation_continuity = ConversationContinuityAnalyzer()
_intent_state_service = StreamerIntentStateService(db_manager)
_persona_evidence_selector = PersonaEvidenceSelector(persona_catalog)


def select_damaku_with_lock(original_func):
    """装饰器：为select_damaku函数添加锁"""
    @wraps(original_func)
    async def wrapper(*args, **kwargs):
        async with _damaku_lock:
            return await original_func(*args, **kwargs)
    return wrapper


class PersonaEngine:
    """人格引擎"""
    
    def __init__(self, repository: PersonaStateRepository | None = None):
        self.ai_service = ai_service
        self.repository = repository or DatabasePersonaStateRepository(db_manager)
        
        # 尝试从数据库读取最新的人格状态
        latest_state = self.repository.get_latest_persona_state()
        if latest_state:
            # 使用数据库中的状态
            self.state = PersonaState(
                mood=latest_state['mood'],
                darkness=latest_state['darkness'],
                stress=latest_state['stress']
            )
            logger.info(f"人格引擎启动成功，从数据库加载状态: mood={self.state.mood:.2f}, stress={self.state.stress:.2f}, darkness={self.state.darkness:.2f}")
        else:
            # 使用默认状态
            self.state = PersonaState(
                mood=settings.persona.initial_mood,
                darkness=settings.persona.initial_darkness,
                stress=settings.persona.initial_stress
            )
            logger.info(f"人格引擎启动成功，使用默认状态: mood={self.state.mood:.2f}, stress={self.state.stress:.2f}, darkness={self.state.darkness:.2f}")

        latest_internal_state = self.repository.get_latest_internal_persona_state()
        self.internal_state = (
            InternalPersonaState(**latest_internal_state)
            if latest_internal_state else InternalPersonaState()
        )
        
        self.behavior = PersonaBehavior(
            reply_aggressiveness=settings.persona.reply_aggressiveness,
            ignore_probability=settings.persona.ignore_probability
        )
        emotion_manager.restore_history(self.repository.get_recent_reply_emotions(limit=24))

    def _state_repository(self) -> PersonaStateRepository:
        """兼容少量绕过构造器的旧测试/扩展，正式实例始终显式持有仓储。"""
        repository = getattr(self, "repository", None)
        if repository is None:
            repository = DatabasePersonaStateRepository(db_manager)
            self.repository = repository
        return repository
    
    def update_state(self, delta: EmotionDelta, dynamics_context: Optional[DynamicsContext] = None):
        """更新人格状态"""
        if dynamics_context and dynamics_context.source != "silence":
            delta = persona_dynamics.apply(self.state, delta, dynamics_context)
        elif dynamics_context:
            # 静默归约本身已包含基线恢复；这里只记录有限冷场余波，避免双重回归。
            persona_dynamics.record_silence_afterglow(dynamics_context)

        self.state.mood = max(0.0, min(1.0, self.state.mood + delta.mood))
        self.state.stress = max(0.0, min(1.0, self.state.stress + delta.stress))
        self.state.darkness = max(0.0, min(1.0, self.state.darkness + delta.darkness))
        logger.debug(f"人格状态更新: mood={self.state.mood:.2f}, stress={self.state.stress:.2f}, darkness={self.state.darkness:.2f}")
        
        # 保存到数据库
        self._state_repository().save_persona_state(
            self.state.mood, self.state.stress, self.state.darkness
        )

    def update_internal_state(self, delta: InternalStateDelta) -> None:
        """更新并持久化仅供后端使用的细粒度状态。"""
        self.internal_state = internal_state_dynamics.project(
            self.internal_state, delta, record=True
        )
        self._save_internal_state()

    def activate_stream_affect_anchor(
        self,
        stream_session_id: str,
        *,
        mood_bias: Optional[Dict[str, float]] = None,
        sources: Optional[Dict[str, Any]] = None,
    ) -> Optional[PersonaAffectAnchor]:
        """恢复或建立本场回归锚点；主题偏置只在创建时计入一次。"""
        normalized_session_id = str(stream_session_id or "").strip()
        if not normalized_session_id:
            return None
        bias = mood_bias if isinstance(mood_bias, dict) else {}
        base = persona_dynamics.baseline
        anchor_values = {
            "mood": max(0.0, min(1.0, base.mood + float(bias.get("mood", 0.0) or 0.0))),
            "stress": max(0.0, min(1.0, base.stress + float(bias.get("stress", 0.0) or 0.0))),
            "darkness": max(0.0, min(1.0, base.darkness + float(bias.get("darkness", 0.0) or 0.0))),
        }
        safe_sources = dict(sources or {})
        safe_sources.setdefault("kind", "stream_session")
        try:
            row = self._state_repository().get_or_create_persona_affect_anchor(
                stream_session_id=normalized_session_id,
                **anchor_values,
                sources=safe_sources,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        except AttributeError:
            # 兼容旧测试/扩展注入的极简仓储；正式 SQLite 路径始终持久化。
            row = {
                "stream_session_id": normalized_session_id,
                **anchor_values,
                "sources": safe_sources,
                "version": 1,
            }
        anchor = PersonaAffectAnchor.from_mapping(row)
        if anchor:
            persona_dynamics.activate_anchor(anchor)
        return anchor

    def clear_stream_affect_anchor(self) -> None:
        """场次结束后不再沿用前一场的主题/场景回归目标。"""
        persona_dynamics.clear_anchor()

    def refresh_stream_affect_anchor(
        self,
        stream_session_id: str,
        *,
        mood_bias: Optional[Dict[str, float]] = None,
        sources: Optional[Dict[str, Any]] = None,
        audience_sentiment: float = 0.0,
        room_sample_count: int = 0,
        danmaku_rate: int = 0,
    ) -> Optional[PersonaAffectAnchor]:
        """仅以足够的房间聚合样本，低幅更新本场回归锚点。

        当前单条弹幕、账号资料和模型输出均不在此入口中，避免把瞬时互动误写成
        长时人格事实。
        """
        anchor = self.activate_stream_affect_anchor(
            stream_session_id, mood_bias=mood_bias, sources=sources,
        )
        if not anchor:
            return None
        tuning = persona_dynamics.tuning
        base = persona_dynamics.baseline
        bias = mood_bias if isinstance(mood_bias, dict) else {}
        sample_count = max(0, int(room_sample_count or 0))
        sentiment = max(-1.0, min(1.0, float(audience_sentiment or 0.0)))
        rate = max(0, int(danmaku_rate or 0))
        enough_room_evidence = sample_count >= tuning.anchor_min_room_samples
        load_scale = min(1.0, rate / max(1, tuning.anchor_load_rate_reference))

        mood = base.mood + float(bias.get("mood", 0.0) or 0.0)
        stress = base.stress + float(bias.get("stress", 0.0) or 0.0)
        darkness = base.darkness + float(bias.get("darkness", 0.0) or 0.0)
        candidate_sources = dict(sources or {})
        candidate_sources["kind"] = "stream_session"
        if enough_room_evidence:
            mood += sentiment * tuning.anchor_room_mood_max
            stress += -sentiment * tuning.anchor_room_stress_max
            darkness += -sentiment * tuning.anchor_room_darkness_max
            stress += load_scale * tuning.anchor_load_stress_max
            candidate_sources["room_signal"] = (
                "positive" if sentiment >= 0.2 else "negative" if sentiment <= -0.2 else "neutral"
            )
            candidate_sources["load_band"] = (
                "busy" if load_scale >= 0.6 else "active" if load_scale >= 0.25 else "calm"
            )
        else:
            candidate_sources["room_signal"] = "insufficient_aggregate"
            candidate_sources["load_band"] = "insufficient_aggregate"

        candidate = {
            "mood": max(0.0, min(1.0, mood)),
            "stress": max(0.0, min(1.0, stress)),
            "darkness": max(0.0, min(1.0, darkness)),
        }
        max_delta = max(
            abs(candidate[axis] - getattr(anchor, axis))
            for axis in ("mood", "stress", "darkness")
        )
        scene_source_changed = any(
            candidate_sources.get(key) != anchor.sources.get(key)
            for key in ("daily_theme_id", "special_theme_id", "activity_id")
        )
        # 聚合信号必须跨过最小差值才更新，形成简单滞回，避免在阈值附近来回写库。
        if not scene_source_changed and max_delta < tuning.anchor_update_min_delta:
            return anchor
        if anchor.version >= tuning.anchor_max_updates_per_stream:
            return anchor
        try:
            row = self._state_repository().update_persona_affect_anchor(
                stream_session_id=anchor.stream_session_id,
                expected_version=anchor.version,
                **candidate,
                sources=candidate_sources,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        except AttributeError:
            row = None
        updated = PersonaAffectAnchor.from_mapping(row)
        if updated:
            persona_dynamics.activate_anchor(updated)
            return updated
        return anchor

    def _save_internal_state(self) -> None:
        self._state_repository().save_internal_persona_state(
            **self.internal_state.model_dump()
        )

    def get_event_states(self) -> tuple[PersonaState, InternalPersonaState]:
        """为人格事件流水线提供当前公开状态和内部状态。"""
        return self.state, self.internal_state

    def apply_event_mutation(self, mutation: PersonaMutation) -> None:
        """统一提交事件归约结果。"""
        if any(abs(value) > 1e-9 for value in mutation.emotion_delta.model_dump().values()):
            self.update_state(mutation.emotion_delta, dynamics_context=mutation.dynamics_context)
        if any(abs(value) > 1e-9 for value in mutation.internal_delta.model_dump().values()):
            self.update_internal_state(mutation.internal_delta)

    async def _commit_semantic_impact(
        self,
        danmaku_id: str,
        raw_delta: EmotionDelta,
        internal_delta: InternalStateDelta,
        dynamics_context: DynamicsContext,
        stream_session_id: str | None = None,
    ) -> None:
        event_identity = {}
        if stream_session_id and danmaku_id and danmaku_id != "legacy_analysis":
            source_event_id = f"danmaku:{stream_session_id}:{danmaku_id}"
            event_identity = {
                "event_id": f"semantic-impact:{stream_session_id}:{danmaku_id}",
                "source_event_id": source_event_id,
            }
        await persona_event_pipeline.publish(
            SemanticImpactAnalyzedEvent(
                **event_identity,
                danmaku_id=danmaku_id,
                raw_delta=raw_delta,
                internal_delta=internal_delta,
                dynamics_context=dynamics_context,
                source="persona_impact_analyzer",
                platform_message_id=danmaku_id,
            ),
            state=self.state,
            internal_state=self.internal_state,
            mutation_handler=self.apply_event_mutation,
        )
    
    def reset_state(self):
        """重置人格状态"""
        self.state = PersonaState(
            mood=settings.persona.initial_mood,
            darkness=settings.persona.initial_darkness,
            stress=settings.persona.initial_stress
        )
        logger.info(f"人格状态已重置: mood={self.state.mood:.2f}, stress={self.state.stress:.2f}, darkness={self.state.darkness:.2f}")
        
        # 保存到数据库
        self._state_repository().save_persona_state(
            self.state.mood, self.state.stress, self.state.darkness
        )
        self.internal_state = InternalPersonaState()
        self._save_internal_state()
        self.clear_stream_affect_anchor()
    
    @select_damaku_with_lock
    async def select_damaku(self, message_history: List[Dict[str, Any]]) -> Optional[PersonaDecision]:
        """选择弹幕并做出决策"""
        try:
            logger.debug("尝试选择弹幕...")
            if len(message_history) < 2:
                logger.debug("弹幕数量小于 2，跳过选择")
                return None
            
            persona_state_dict = self.state.model_dump()
            messages, response_format = persona_decision_prompt_builder.generate_prompt(
                persona_state=persona_state_dict,
                danmaku_list=list(message_history)
            )
            logger.info(f"生成的提示: {messages}")
            
            
            result = await self.ai_service.run(
                messages=messages,
                role="danmaku_selector",
                response_format=response_format,
                timeout=settings.ai.danmaku_selector_timeout,
            )
            
            logger.debug(f"选择结果: {result.get('reply', '')}")
            
            reply_text = result.get('reply', '')
            if reply_text:
                import json
                decision_data = json.loads(reply_text)
                decision = PersonaDecision(**decision_data)
                self.update_state(decision.emotion_delta)
                return decision
            
        except Exception as e:
            logger.error(f"选择弹幕失败: {e}")
        
        return None
    
    async def generate_reply(self, danmaku_message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """生成AI回复"""
        reply_started_at = perf_counter()
        reply_path = "sc" if danmaku_message.get("_is_sc_danmaku") else "normal"
        is_moderation_response = bool(danmaku_message.get("_is_moderation_response"))
        # 只有传输层显式附带的场次才启用持久化 claim。内部测试、离线回放
        # 和旧调用者可能反复使用固定消息 id，不能借用“当前排期”误判为线上事件。
        claim_stream_session_id = danmaku_message.get("_stream_session_id")
        stream_session_id = (
            claim_stream_session_id or stream_metadata_pusher.get_current_stream_session_id()
        )
        processing_claim_token = None
        danmaku_id = str(danmaku_message.get("danmakuID", "") or "")
        if (
            reply_path == "normal"
            and not is_moderation_response
            and claim_stream_session_id
            and danmaku_id
        ):
            processing_claim_token = uuid4().hex
            claimed = await asyncio.to_thread(
                db_manager.claim_danmaku_processing,
                stream_session_id=str(claim_stream_session_id),
                source_type="normal",
                danmaku_id=danmaku_id,
                claim_token=processing_claim_token,
                lease_seconds=settings.rate_limit.ai_reply_claim_lease_seconds,
            )
            if not claimed:
                logger.info(
                    "重复普通弹幕处理已在模型调用前拒绝: session=%s danmaku_id=%s",
                    claim_stream_session_id, danmaku_id,
                )
                return None
        logger.info(f"[DEBUG] generate_reply 开始执行，弹幕: {danmaku_message.get('message', '')[:50]}...")
        analysis = None
        dynamics_context = None
        raw_delta = None
        internal_delta = None
        state_commit_started = False
        internal_commit_started = False
        relationship_reply_recorded = False
        long_term_memory_recorded = False
        emotion_history_recorded = False
        retrieved_qa = None
        long_term_memory_context = None
        conversation_context = None
        mood_before = self.state.mood
        stress_before = self.state.stress
        darkness_before = self.state.darkness
        
        try:
            # 获取弹幕记忆上下文
            context_started_at = perf_counter()
            mark_current("context_started_at")
            logger.debug(f"[DEBUG] 开始获取记忆上下文...")
            memory_context = await danmaku_memory_manager.get_memory_context(limit=10)
            logger.debug(f"[DEBUG] 记忆上下文获取完成")
            
            # 获取已回复的弹幕列表
            logger.debug(f"[DEBUG] 开始获取已回复弹幕...")
            replied_danmaku = await danmaku_pool.get_replied_danmaku(limit=20)
            logger.debug(f"[DEBUG] 已回复弹幕获取完成，数量: {len(replied_danmaku)}")
            
            # 构建已回复弹幕的上下文
            replied_context = []
            for item in replied_danmaku[:5]:
                context_item = {
                    "nickname": item.nickname,
                    "message": item.message,
                    "timestamp": item.timestamp.isoformat()
                }
                # 添加回复内容（如果有）
                if item.reply_content:
                    context_item["reply_content"] = item.reply_content
                replied_context.append(context_item)
            memory_context["replied_danmaku"] = replied_context
            relationship = await audience_relationship_manager.get(
                danmaku_message.get('nickname', '匿名宅宅'),
                identity=danmaku_message.get('_viewer_identity'),
            )
            memory_context["viewer_relationship"] = relationship.model_dump()
            if is_moderation_response:
                memory_context["moderation_action"] = danmaku_message.get(
                    "_moderation_action", "warning"
                )
            memory_context["nickname_identity"] = (
                nickname_history_context_manager.build_for_reply(
                    danmaku_message.get('_viewer_identity')
                )
            )
            long_term_memory_context = self._retrieve_long_term_context(
                danmaku_message.get('_viewer_identity'),
                danmaku_message.get('message', ''),
            )
            memory_context["viewer_long_term_memory"] = long_term_memory_context
            memory_context["streamer_episodic_memory"] = episodic_memory_manager.retrieve_for_reply(
                danmaku_message.get('_viewer_identity'), danmaku_message.get('message', '')
            )
            memory_context["danmaku_rate"] = persona_event_pipeline.current_danmaku_rate
            memory_context["daily_stream_theme"] = (
                stream_metadata_pusher.get_theme_prompt_context()
            )
            memory_context["current_streamer_activity"] = (
                stream_metadata_pusher.get_activity_prompt_context()
            )
            self._apply_mainline_prompt_context(
                memory_context, stream_metadata_pusher.get_mainline_prompt_context()
            )
            memory_context["previous_stream_summary"] = (
                stream_metadata_pusher.get_previous_session_summary_prompt_context(
                    danmaku_message.get("message", "")
                )
            )
            language_detection = language_detector.detect(
                danmaku_message.get("message", "")
            )
            english_surprise_joke_service.record_detection(language_detection)
            english_surprise_joke = english_surprise_joke_service.should_offer(
                detection=language_detection,
                identity=danmaku_message.get("_viewer_identity"),
                stream_session_id=stream_metadata_pusher.get_current_stream_session_id(),
                event_id=danmaku_message.get("danmakuID", ""),
            )
            memory_context["reply_language"] = (
                reply_language_policy.build_prompt_context(
                    language_detection,
                    english_surprise_joke=english_surprise_joke,
                )
            )
            logger.info(
                "弹幕语言检测: language=%s confidence=%.2f mixed=%s reliable=%s",
                language_detection.language,
                language_detection.confidence,
                language_detection.is_mixed,
                language_detection.is_reliable,
            )
            conversation_context = self._build_direct_conversation_context(
                long_term_context=long_term_memory_context,
                replied_danmaku=replied_danmaku,
                identity=danmaku_message.get('_viewer_identity'),
                current_message=danmaku_message.get('message', ''),
                nickname=danmaku_message.get('nickname', ''),
            )
            stream_session_id = (
                stream_session_id
                or stream_metadata_pusher.get_current_stream_session_id()
            )
            intent_state = (
                _intent_state_service.get_active(stream_session_id)
                if stream_session_id else None
            )
            next_beat_shadow_candidate = (
                intent_candidate_shadow_service.consume_next_beat_candidate(intent_state)
            )
            if next_beat_shadow_candidate is not None:
                logger.debug(
                    "已观察到上一拍版本匹配的意图候选（影子模式，不改变本轮计划）: %s",
                    next_beat_shadow_candidate.interaction_mode.value,
                )
            reply_plan = response_planner.plan(
                message=danmaku_message.get("message", ""),
                is_sc=bool(danmaku_message.get("_is_sc_danmaku")),
                conversation_context=conversation_context,
                activity=memory_context.get("current_streamer_activity"),
                internal_state=self.internal_state.model_dump(),
                language_reliable=language_detection.is_reliable,
                requires_boundary=bool(danmaku_message.get("_requires_boundary")),
            )
            self._apply_reply_plan_prompt_context(memory_context, reply_plan)
            # P30 prompt RAM：先按身份结清「等这个人回话」，再装配注入层。
            # 全部同步内存操作，服务内部自带 try/except，绝不影响本轮回复。
            ram_subject_id = self._viewer_subject_id(
                danmaku_message.get("_viewer_identity")
            )
            prompt_ram_service.resolve_incoming(
                subject_id=ram_subject_id,
                stream_session_id=stream_session_id or "",
            )
            memory_context["prompt_ram"] = prompt_ram_service.build_for_reply(
                subject_id=ram_subject_id,
                stream_session_id=stream_session_id or "",
            )
            # 情绪分析只拿这一个服务端可验证的布尔量，不透传任何念头原文。
            memory_context["prompt_ram_awaiting_current_viewer"] = (
                prompt_ram_service.awaiting_current_viewer(
                    subject_id=ram_subject_id,
                    stream_session_id=stream_session_id or "",
                )
            )
            impact_scene_context = self._build_impact_scene_context(memory_context)
            # P22.B 当前只建立并提交可审计状态；提示词分层注入留给 P22.C，
            # 避免在未完成预算裁剪前把新的背景重新堆回模型上下文。
            reply_timing_metrics.record(
                "context", (perf_counter() - context_started_at) * 1000,
                path=reply_path,
            )
            mark_current("context_finished_at")

            # QA 只为最终回复提供人设证据；情感分析使用完整人格、当前状态与
            # 已核验的直接上下文，两者不再互相等待。
            if conversation_context and conversation_context.get("depends_on_previous"):
                # 三条分支各自标注：没有它，快照里「缺 qa_ms」既可能是跳过、
                # 也可能是失败，无法区分。
                note_current("stage_mode", "qa_skipped")
                retrieved_qa = []
                reply_timing_metrics.record(
                    "qa_selection", 0, path=reply_path,
                    outcome="skipped", model_role="qa",
                )
                logger.info(
                    "当前弹幕依赖上一轮直接语义，本轮跳过人设QA检索: %r",
                    danmaku_message.get('message', '')[:40],
                )
                impact_started_at = perf_counter()
                mark_current("impact_started_at")
                try:
                    analysis = await persona_impact_analyzer.analyze_danmaku_impact(
                        danmaku_message.get('message', ''),
                        self.state,
                        retrieved_qa=[],
                        conversation_context=conversation_context,
                        **impact_scene_context,
                    )
                finally:
                    mark_current("impact_finished_at")
                    reply_timing_metrics.record(
                        "impact_analysis", (perf_counter() - impact_started_at) * 1000,
                        path=reply_path, model_role="impact",
                    )
            elif settings.ai.parallel_context_analysis:
                note_current("stage_mode", "parallel")
                parallel_started = perf_counter()
                mark_current("parallel_started_at")
                async def timed_qa():
                    started_at = perf_counter()
                    mark_current("qa_started_at")
                    try:
                        return await persona_qa_selector.select(
                            danmaku_message.get('message', ''), self.state,
                            top_k=3, conversation_context=conversation_context,
                        )
                    finally:
                        mark_current("qa_finished_at")
                        reply_timing_metrics.record(
                            "qa_selection", (perf_counter() - started_at) * 1000,
                            path=reply_path, model_role="qa",
                        )

                async def timed_impact():
                    started_at = perf_counter()
                    mark_current("impact_started_at")
                    try:
                        return await persona_impact_analyzer.analyze_danmaku_impact(
                            danmaku_message.get('message', ''), self.state,
                            retrieved_qa=[], conversation_context=conversation_context,
                            **impact_scene_context,
                        )
                    finally:
                        mark_current("impact_finished_at")
                        reply_timing_metrics.record(
                            "impact_analysis", (perf_counter() - started_at) * 1000,
                            path=reply_path, model_role="impact",
                        )

                retrieved_qa, analysis = await asyncio.gather(
                    timed_qa(), timed_impact(),
                )
                mark_current("parallel_finished_at")
                logger.info(
                    "QA选择与情感影响并行完成，关键路径耗时=%dms",
                    round((perf_counter() - parallel_started) * 1000),
                )
            else:
                note_current("stage_mode", "serial")
                qa_started_at = perf_counter()
                mark_current("qa_started_at")
                try:
                    retrieved_qa = await persona_qa_selector.select(
                        danmaku_message.get('message', ''), self.state,
                        top_k=3, conversation_context=conversation_context,
                    )
                finally:
                    mark_current("qa_finished_at")
                    reply_timing_metrics.record(
                        "qa_selection", (perf_counter() - qa_started_at) * 1000,
                        path=reply_path, model_role="qa",
                    )
                impact_started_at = perf_counter()
                mark_current("impact_started_at")
                try:
                    analysis = await persona_impact_analyzer.analyze_danmaku_impact(
                        danmaku_message.get('message', ''), self.state,
                        retrieved_qa=[], conversation_context=conversation_context,
                        **impact_scene_context,
                    )
                finally:
                    mark_current("impact_finished_at")
                    reply_timing_metrics.record(
                        "impact_analysis", (perf_counter() - impact_started_at) * 1000,
                        path=reply_path, model_role="impact",
                    )

            # 在生成回复前只分析一次当前弹幕，用投影状态指导本轮即时反应。
            # §4 需要「空召回占比」的线上口径，所以这里顺手记条数（不记内容）。
            note_current("qa_hits", len(retrieved_qa or []))
            # §5 要能看出这一条的评价是模型给的还是掉进了本地兜底。
            note_current("appraisal_source", getattr(analysis, "appraisal_source", "none"))
            reaction_state = self.state.model_copy(deep=True)
            reaction_internal_state = self.internal_state.model_copy(deep=True)
            if analysis:
                raw_delta = persona_impact_analyzer.get_emotion_delta(analysis)
                dynamics_context = persona_dynamics.build_context(
                    memory_context=memory_context,
                    danmaku_message=danmaku_message.get('message', ''),
                    source="immediate_reaction"
                )
                reaction_delta = persona_dynamics.preview(
                    self.state,
                    raw_delta,
                    dynamics_context
                )
                reaction_state = persona_dynamics.project_state(self.state, reaction_delta)
                internal_delta = internal_state_dynamics.derive_delta(
                    self.internal_state, analysis, dynamics_context
                )
                reaction_internal_state = internal_state_dynamics.project(
                    self.internal_state, internal_delta
                )
            if (
                settings.persona.reply_plan_injection_enabled
                and settings.ai.intent_shadow_enabled
                and settings.ai.intent_candidate_apply_enabled
            ):
                reply_plan = intent_candidate_shadow_service.merge_completed_analysis(
                    reply_plan, analysis
                )
                self._apply_reply_plan_prompt_context(memory_context, reply_plan)
            asyncio.create_task(intent_candidate_shadow_service.observe(
                analysis=analysis, plan=reply_plan,
                is_sc=bool(danmaku_message.get("_is_sc_danmaku")),
                event_id=danmaku_message.get("danmakuID", ""),
                intent_version=intent_state.version if intent_state else None,
                stream_session_id=stream_session_id,
            ))

            current_persona_state = reaction_state.model_dump()
            current_internal_state = reaction_internal_state.model_dump()
            emotion_context = emotion_manager.build_prompt_context(
                reaction_state.mood,
                reaction_state.stress,
                reaction_state.darkness,
            )
            logger.debug(f"[DEBUG] 本轮即时反应状态: {current_persona_state}")

            configured_prompt_mode = settings.persona.prompt_mode
            active_prompt_mode = resolve_prompt_mode(
                configured_prompt_mode,
                settings.persona.prompt_rollout_percent,
                stream_session_id or danmaku_message.get("danmakuID", ""),
            )
            persona_evidence = []
            voice_exemplars = []
            style_vector = None
            # Legacy 默认路径不触碰 Catalog 选择和新状态投影，保证迁移组件故障
            # 不会改变原回复主链。
            if active_prompt_mode != "legacy":
                try:
                    relationship = memory_context.get("viewer_relationship") or {}
                    familiarity = float(relationship.get("familiarity", 0.0) or 0.0)
                    trust = float(relationship.get("trust", 0.0) or 0.0)
                    relationship_scope = (
                        "trusted"
                        if familiarity >= 0.7 and trust >= 0.65
                        else "familiar" if familiarity >= 0.3 else "public"
                    )
                    persona_selection = _persona_evidence_selector.select_from_legacy_matches(
                        retrieved_qa,
                        evidence_limit=settings.persona.catalog_evidence_limit,
                        exemplar_enabled=settings.persona.catalog_exemplar_enabled,
                        exemplar_limit=settings.persona.catalog_exemplar_limit,
                        relationship_scope=relationship_scope,
                    )
                    persona_evidence = list(persona_selection.evidence)
                    voice_exemplars = list(persona_selection.exemplars)
                    style_vector = build_style_vector(
                        current_persona_state, current_internal_state
                    )
                except Exception:
                    persona_prompt_metrics.record("catalog_selection_fallback_legacy")
                    logger.exception("Persona Catalog 选择失败，本轮回退 Legacy Prompt")
                    active_prompt_mode = "legacy"
            
            logger.debug(f"[DEBUG] 开始生成提示词...")
            prompt_started_at = perf_counter()
            mark_current("prompt_started_at")
            prompt_kwargs = dict(
                additional_context=danmaku_message.get('message', ''),
                persona_state=current_persona_state,
                memory_context=memory_context,
                internal_state=current_internal_state,
                emotion_context=emotion_context,
                retrieved_qa=retrieved_qa,
                persona_evidence=persona_evidence,
                voice_exemplars=voice_exemplars,
                persona_style_vector=style_vector,
                conversation_context=conversation_context,
                is_sc_danmaku=bool(danmaku_message.get("_is_sc_danmaku")),
                moderation_action=(
                    str(danmaku_message.get("_moderation_action", "warning"))
                    if is_moderation_response else None
                ),
            )
            try:
                messages, response_format = streamer_reply_prompt_builder.generate_prompt(
                    **prompt_kwargs,
                    prompt_mode=(
                        "legacy" if active_prompt_mode == "shadow" else active_prompt_mode
                    ),
                )
            except Exception:
                if active_prompt_mode != "catalog":
                    raise
                persona_prompt_metrics.record("catalog_build_fallback_legacy")
                logger.exception("Catalog Prompt 构建失败，本轮回退 Legacy Prompt")
                messages, response_format = streamer_reply_prompt_builder.generate_prompt(
                    **prompt_kwargs, prompt_mode="legacy"
                )
            if active_prompt_mode == "shadow":
                try:
                    shadow_messages, _ = streamer_reply_prompt_builder.generate_prompt(
                        **prompt_kwargs,
                        prompt_mode="catalog",
                    )
                    persona_prompt_metrics.record_shadow_comparison(
                        "\n".join(item["content"] for item in messages),
                        "\n".join(item["content"] for item in shadow_messages),
                    )
                except Exception:
                    persona_prompt_metrics.record("shadow_build_failed")
                    logger.exception("Catalog Shadow 构建失败；Legacy 回复继续")
            persona_prompt_metrics.record(f"prompt_mode_{active_prompt_mode}")
            mark_current("prompt_finished_at")
            reply_timing_metrics.record(
                "prompt_build", (perf_counter() - prompt_started_at) * 1000,
                path=reply_path,
            )
            logger.debug(f"[DEBUG] 提示词生成完成，消息数量: {len(messages)}")
            
            logger.info(f"开始生成回复，弹幕: {danmaku_message.get('message', '')[:30]}...")
            
            reply_model_started_at = perf_counter()
            mark_current("reply_llm_started_at")
            try:
                result = await self.ai_service.run(
                    messages=messages,
                    role="default",
                    model=settings.ai.default_model,
                    model_mode="role_hint",
                    response_format=response_format
                )
            finally:
                mark_current("reply_llm_finished_at")
                reply_timing_metrics.record(
                    "reply_model", (perf_counter() - reply_model_started_at) * 1000,
                    path=reply_path, model_role="reply",
                )
            
            reply_text = result.get('reply', '')
            logger.debug(f"AI返回内容: {reply_text[:100]}...")
            
            validation_started_at = perf_counter()
            reply_data = None
            reply_generated = False
            raw_thoughts = None
            if reply_text:
                import json
                try:
                    # 清理JSON标记（移除 ```json 和 ``` 包裹）
                    cleaned_text = reply_text.strip()
                    
                    # 移除 ```json 开头
                    if cleaned_text.startswith('```json'):
                        cleaned_text = cleaned_text[7:]
                    # 或者移除 ``` 开头（有些模型可能用这个）
                    elif cleaned_text.startswith('```'):
                        cleaned_text = cleaned_text[3:]
                    
                    # 移除结尾的 ```
                    if cleaned_text.endswith('```'):
                        cleaned_text = cleaned_text[:-3]
                    
                    # 再次清理空白
                    cleaned_text = cleaned_text.strip()
                    
                    # 尝试解析JSON
                    reply_data = json.loads(cleaned_text)
                    # P30：想法就地剥离。pop 而非 read —— 这样下游的 WS 广播与
                    # record_reply 入库天然拿不到模型自由念头，不必在各处补防。
                    if isinstance(reply_data, dict):
                        raw_thoughts = reply_data.pop("thoughts", None)
                    reply_generated = True
                    logger.debug(f"JSON解析成功: {json.dumps(reply_data, ensure_ascii=False)[:100]}")
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON解析失败，使用默认回复: {e}")
                    logger.warning(f"模型返回的原始内容: {repr(reply_text)}")
                    # JSON解析失败时，提供默认回复
                    reply_data = self._get_default_reply(danmaku_message)
            else:
                # 没有返回内容时，使用默认回复
                reply_data = self._get_default_reply(danmaku_message)

            if reply_generated and not stream_metadata_pusher.reply_preserves_activity_fact(
                reply_data
            ):
                logger.warning("模型回复擅自声明未提交的活动切换，已拒绝该回复")
                reply_generated = False
                reply_data = self._get_default_reply(danmaku_message)

            if danmaku_message.get("_is_sc_danmaku") and (
                not reply_generated or not self._is_displayable_reply(reply_data)
            ):
                logger.warning("SC 模型回复为空、格式错误或不可展示，本次不提交 replied")
                return {
                    'reply_data': None,
                    'reply_generated': False,
                    'generation_failure_code': 'invalid_ai_reply',
                    'analysis': analysis,
                    'mood_before': mood_before,
                    'stress_before': stress_before,
                    'darkness_before': darkness_before,
                    'mood_after': self.state.mood,
                    'stress_after': self.state.stress,
                    'darkness_after': self.state.darkness,
                }

            reply_data = emotion_manager.diversify_reply(
                reply_data,
                reaction_state.mood,
                reaction_state.stress,
                reaction_state.darkness,
            )
            mark_current("validation_finished_at")
            reply_timing_metrics.record(
                "output_validation", (perf_counter() - validation_started_at) * 1000,
                path=reply_path,
            )
            
            # 回复确定后提交同一次分析产生的长期变化，避免重复计算。
            commit_started_at = perf_counter()
            if reply_data and raw_delta and dynamics_context:
                state_commit_started = True
                internal_commit_started = internal_delta is not None
                await self._commit_semantic_impact(
                    danmaku_message.get('danmakuID', ''),
                    raw_delta,
                    internal_delta or InternalStateDelta(),
                    dynamics_context,
                    stream_session_id=str(claim_stream_session_id) if claim_stream_session_id else None,
                )
                self._log_impact_analysis(danmaku_message.get('message', ''), analysis)
            # 人格提交与记忆提交分开记：§2 要求能分别看到两段提交的耗时，
            # 也顺便证明提交顺序（人格 → 记忆）没有被并行打乱。
            mark_current("persona_commit_finished_at")
            if reply_data and not is_moderation_response:
                long_term_memory_recorded = self._record_long_term_exchange(
                    danmaku_message,
                    reply_data,
                    analysis,
                    long_term_memory_context,
                )
                await audience_relationship_manager.record_reply(
                    danmaku_message.get('nickname', '匿名宅宅'), analysis,
                    identity=danmaku_message.get('_viewer_identity'),
                    conversation_transition=(long_term_memory_context or {}).get(
                        "transition"
                    ),
                )
                relationship_reply_recorded = True
                if not danmaku_message.get("_is_sc_danmaku"):
                    try:
                        episodic_memory_manager.capture_reply(
                            stream_session_id=stream_session_id,
                            danmaku_id=danmaku_message.get('danmakuID', ''),
                            message=danmaku_message.get('message', ''),
                            identity=danmaku_message.get('_viewer_identity'),
                            analysis=analysis,
                            is_sc=False,
                        )
                    except Exception as exc:
                        logger.warning("记录 P24 情景记忆候选失败: %s", exc)
            if reply_data:
                emotion_manager.record_emotions(reply_data.get("emotions", []))
                emotion_history_recorded = True
            if reply_generated and self._is_displayable_reply(reply_data) and intent_state:
                committed_intent = _intent_state_service.commit_after_reply(
                    intent_state, **reply_plan.to_intent_update()
                )
                if committed_intent is None:
                    logger.debug("主播心智状态版本冲突，本轮不覆盖较新的节拍")
                else:
                    intent_candidate_shadow_service.mark_reply_success(
                        event_id=danmaku_message.get("danmakuID", ""),
                        stream_session_id=stream_session_id or "",
                        base_intent_version=intent_state.version,
                        committed_intent_version=committed_intent.version,
                    )
            # P30：只有核验通过的可展示回复才允许写工作记忆。这里不要求
            # intent_state 存在（没有活动场次时它是 None，但念头照样该记），
            # reply_generated 本身已经包含了活动事实未被篡改的校验。
            if reply_generated and raw_thoughts and self._is_displayable_reply(reply_data):
                try:
                    prompt_ram_service.harvest(
                        raw_thoughts,
                        subject_id=self._viewer_subject_id(
                            danmaku_message.get('_viewer_identity')
                        ),
                        nickname=danmaku_message.get('nickname', ''),
                        stream_session_id=stream_session_id or "",
                        danmaku_id=danmaku_message.get('danmakuID', ''),
                    )
                except Exception as exc:
                    logger.warning("采集 P30 工作记忆失败: %s", exc)
            mark_current("memory_commit_finished_at")
            reply_timing_metrics.record(
                "state_commit", (perf_counter() - commit_started_at) * 1000,
                path=reply_path,
            )
            
            # 返回回复数据和分析结果
            return {
                'reply_data': reply_data,
                'reply_generated': reply_generated,
                'analysis': analysis,
                'mood_before': mood_before,
                'stress_before': stress_before,
                'darkness_before': darkness_before,
                'mood_after': self.state.mood,
                'stress_after': self.state.stress,
                'darkness_after': self.state.darkness
            }
            
        except Exception as e:
            logger.error(f"生成回复失败: {e}")
            if danmaku_message.get("_is_sc_danmaku"):
                return {
                    'reply_data': None,
                    'reply_generated': False,
                    'generation_failure_code': 'reply_generation_failed',
                    'analysis': analysis,
                    'mood_before': mood_before,
                    'stress_before': stress_before,
                    'darkness_before': darkness_before,
                    'mood_after': self.state.mood,
                    'stress_after': self.state.stress,
                    'darkness_after': self.state.darkness,
                }
            # 出错时也提供默认回复
            reply_data = emotion_manager.diversify_reply(
                self._get_default_reply(danmaku_message),
                self.state.mood,
                self.state.stress,
                self.state.darkness,
            )
            
            # 如果异常发生在分析之后，复用已有结果；否则执行一次回退分析。
            if analysis is None:
                analysis = await persona_impact_analyzer.analyze_danmaku_impact(
                    danmaku_message.get('message', ''),
                    self.state,
                    retrieved_qa=retrieved_qa,
                    conversation_context=conversation_context,
                    **self._build_impact_scene_context(memory_context),
                )
            if analysis and raw_delta is None:
                raw_delta = persona_impact_analyzer.get_emotion_delta(analysis)
            if analysis and dynamics_context is None:
                dynamics_context = persona_dynamics.build_context(
                    danmaku_message=danmaku_message.get('message', ''),
                    source="reply_fallback"
                )
            if raw_delta and dynamics_context and not state_commit_started:
                state_commit_started = True
                if analysis and internal_delta is None:
                    internal_delta = internal_state_dynamics.derive_delta(
                        self.internal_state, analysis, dynamics_context
                    )
                internal_commit_started = internal_delta is not None
                await self._commit_semantic_impact(
                    danmaku_message.get('danmakuID', ''),
                    raw_delta,
                    internal_delta or InternalStateDelta(),
                    dynamics_context,
                    stream_session_id=str(claim_stream_session_id) if claim_stream_session_id else None,
                )
                self._log_impact_analysis(danmaku_message.get('message', ''), analysis)
            if analysis and dynamics_context and internal_delta is None:
                internal_delta = internal_state_dynamics.derive_delta(
                    self.internal_state, analysis, dynamics_context
                )
            if internal_delta and not internal_commit_started:
                internal_commit_started = True
                self.update_internal_state(internal_delta)
            if not relationship_reply_recorded and not is_moderation_response:
                if reply_data and not long_term_memory_recorded:
                    long_term_memory_recorded = self._record_long_term_exchange(
                        danmaku_message,
                        reply_data,
                        analysis,
                        long_term_memory_context,
                    )
                await audience_relationship_manager.record_reply(
                    danmaku_message.get('nickname', '匿名宅宅'), analysis,
                    identity=danmaku_message.get('_viewer_identity'),
                    conversation_transition=(long_term_memory_context or {}).get(
                        "transition"
                    ),
                )
                relationship_reply_recorded = True
                if not danmaku_message.get("_is_sc_danmaku"):
                    try:
                        episodic_memory_manager.capture_reply(
                            stream_session_id=stream_metadata_pusher.get_current_stream_session_id(),
                            danmaku_id=danmaku_message.get('danmakuID', ''),
                            message=danmaku_message.get('message', ''),
                            identity=danmaku_message.get('_viewer_identity'),
                            analysis=analysis,
                            is_sc=False,
                        )
                    except Exception as exc:
                        logger.warning("记录 P24 回退回复候选失败: %s", exc)
            if reply_data and not emotion_history_recorded:
                emotion_manager.record_emotions(reply_data.get("emotions", []))
                emotion_history_recorded = True
            
            # 返回回复数据和分析结果
            return {
                'reply_data': reply_data,
                'reply_generated': False,
                'analysis': analysis,
                'mood_before': mood_before,
                'stress_before': stress_before,
                'darkness_before': darkness_before,
                'mood_after': self.state.mood,
                'stress_after': self.state.stress,
                'darkness_after': self.state.darkness
            }
        finally:
            if processing_claim_token:
                try:
                    completed = await asyncio.to_thread(
                        db_manager.complete_danmaku_processing,
                        stream_session_id=str(claim_stream_session_id),
                        source_type="normal",
                        danmaku_id=danmaku_id,
                        claim_token=processing_claim_token,
                    )
                    if not completed:
                        logger.error(
                            "普通弹幕处理 claim 完成失败: session=%s danmaku_id=%s",
                            stream_session_id, danmaku_id,
                        )
                except Exception as claim_error:
                    logger.error(
                        "普通弹幕处理 claim 持久化失败: %s", claim_error,
                        exc_info=True,
                    )
            reply_timing_metrics.record(
                "total", (perf_counter() - reply_started_at) * 1000,
                path=reply_path,
            )

    @staticmethod
    def _is_displayable_reply(reply_data: Any) -> bool:
        if not isinstance(reply_data, dict):
            return False
        sentences = reply_data.get("sentences")
        if not isinstance(sentences, list) or not sentences:
            return False
        return all(
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and bool(item["text"].strip())
            for item in sentences
        )
        
        return None

    def _record_long_term_exchange(
        self,
        danmaku_message: Dict[str, Any],
        reply_data: dict,
        analysis: Any,
        retrieval_context: Optional[dict],
    ) -> bool:
        """长期记忆失败不得破坏本轮直播回复。"""
        try:
            stored = long_term_memory_manager.record_exchange(
                identity=danmaku_message.get('_viewer_identity'),
                danmaku_id=danmaku_message.get('danmakuID', ''),
                viewer_message=danmaku_message.get('message', ''),
                reply_data=reply_data,
                analysis=analysis,
                retrieval_context=retrieval_context,
            )
            return stored is not None
        except Exception as exc:
            logger.error("记录账号长期对话记忆失败: %s", exc, exc_info=True)
            return False

    @staticmethod
    def _viewer_subject_id(identity) -> Optional[str]:
        """从已核验身份取唯一身份键；昵称永远不是身份主键。"""
        subject_id = getattr(identity, "subject_id", None)
        if isinstance(subject_id, str) and subject_id.strip():
            return subject_id.strip()
        return None

    def _retrieve_long_term_context(self, identity, message: str) -> Optional[dict]:
        """长期记忆读取失败时降级为空证据，不阻断模型主回复。"""
        try:
            return long_term_memory_manager.retrieve_for_reply(identity, message)
        except Exception as exc:
            logger.error("读取账号长期对话记忆失败: %s", exc, exc_info=True)
            return None

    def _build_direct_conversation_context(
        self,
        long_term_context: Optional[dict],
        replied_danmaku: Optional[list] = None,
        identity=None,
        current_message: str = "",
        nickname: str = "",
    ) -> Optional[dict]:
        """为语义相关的各个模型阶段提供同一份上一轮直接对话事实。"""
        live_context = self._build_live_direct_context(
            replied_danmaku or [], identity, current_message, nickname
        )
        if live_context:
            return live_context
        if long_term_context:
            previous = long_term_context.get("previous_fragment")
            if previous:
                return {
                    "previous_viewer_message": previous.get("viewer_message", ""),
                    "previous_streamer_reply": previous.get("streamer_reply", ""),
                    "transition": long_term_context.get("transition", "new"),
                    "resolved_reference": long_term_context.get("resolved_reference"),
                    "depends_on_previous": bool(
                        long_term_context.get("depends_on_previous")
                    ),
                    "same_verified_viewer": True,
                    "current_viewer_nickname": nickname,
                    "previous_viewer_nickname": previous.get("nickname", nickname),
                    "identity_scope": "authenticated_account",
                }
        return None

    def _build_live_direct_context(
        self, replied_danmaku: list, identity, current_message: str, nickname: str
    ) -> Optional[dict]:
        """从同一连接身份最近回复恢复游客也可用的直接上一轮。"""
        previous_item = None
        for item in replied_danmaku:
            item_identity = getattr(item, "viewer_identity", None)
            if identity and item_identity:
                if item_identity.subject_id == identity.subject_id:
                    previous_item = item
                    break
            # 没有服务端身份时禁止按昵称关联；同名观众不能共享直接对话。
        if previous_item is None:
            return None
        previous_message = getattr(previous_item, "message", "")
        previous_reply = self._reply_text_from_pool_item(previous_item)
        if not previous_message or not previous_reply:
            return None
        previous_thread = _conversation_continuity.classify(previous_message, None)
        previous = {
            **previous_thread,
            "viewer_message": previous_message,
            "streamer_reply": previous_reply,
        }
        transition = _conversation_continuity.classify(current_message, previous)
        return {
            "previous_viewer_message": previous_message,
            "previous_streamer_reply": previous_reply,
            "transition": transition.get("transition", "new"),
            "resolved_reference": transition.get("resolved_reference"),
            "depends_on_previous": bool(transition.get("depends_on_previous")),
            "source": "live_connection",
            "same_verified_viewer": True,
            "current_viewer_nickname": nickname,
            "previous_viewer_nickname": getattr(previous_item, "nickname", nickname),
            "identity_scope": (
                "authenticated_account" if identity.is_authenticated
                else "guest_connection"
            ),
        }

    @staticmethod
    def _reply_text_from_pool_item(item) -> str:
        import json

        payload = getattr(item, "reply_content", "")
        if not payload:
            return ""
        try:
            parsed = json.loads(payload) if isinstance(payload, str) else payload
        except (TypeError, json.JSONDecodeError):
            return str(payload)[:1000]
        if not isinstance(parsed, dict):
            return ""
        return " ".join(
            str(sentence.get("text", "")).strip()
            for sentence in parsed.get("sentences", [])
            if isinstance(sentence, dict) and sentence.get("text")
        )[:1000]

    def _log_impact_analysis(self, danmaku_message: str, analysis) -> None:
        if not analysis:
            return
        logger.info("AI驱动的人格状态更新:")
        logger.info(f"  情感倾向: {analysis.emotional_tone}")
        logger.info(f"  内容强度: {analysis.content_intensity:.2f}")
        logger.info(f"  上下文相关性: {analysis.context_relevance:.2f}")
        logger.info(f"  关键因素: {', '.join(analysis.key_factors)}")
        self._log_event_appraisal(analysis)
        logger.info(
            f"  动力学后状态: mood={self.state.mood:.2f}, "
            f"stress={self.state.stress:.2f}, darkness={self.state.darkness:.2f}"
        )

    @staticmethod
    def _apply_mainline_prompt_context(memory_context: dict, mainline_context) -> None:
        """灰度关闭时主提示词里不出现本场主线，回退到 P26 之前的上下文形状。"""
        if not settings.stream.mainline_prompt_injection_enabled:
            memory_context.pop("current_stream_mainline", None)
            return
        memory_context["current_stream_mainline"] = mainline_context

    @staticmethod
    def _apply_reply_plan_prompt_context(memory_context: dict, reply_plan) -> None:
        """灰度关闭时不把 P22 计划层写回主提示词，回退到既有回复链。"""
        if not settings.persona.reply_plan_injection_enabled:
            memory_context.pop("reply_plan", None)
            return
        memory_context["reply_plan"] = {
            "interaction_mode": reply_plan.interaction_mode.value,
            "primary_intent": reply_plan.primary_intent.value,
            "energy_level": reply_plan.energy_level,
            "callback_fact": reply_plan.callback_fact,
        }

    @staticmethod
    def _build_impact_scene_context(memory_context: Optional[dict]) -> dict:
        """压缩后端事实供影响分析使用，不含主题、个人记忆或原始 SC。"""
        context = memory_context or {}
        relationship = context.get("viewer_relationship", {}) or {}
        try:
            trust = float(relationship.get("trust", .5) or .5)
            familiarity = float(relationship.get("familiarity", 0) or 0)
        except (TypeError, ValueError):
            trust, familiarity = .5, 0
        if trust < .25:
            boundary = "careful"
        elif familiarity >= .65 and trust >= .6:
            boundary = "established"
        else:
            boundary = "standard"
        return {
            "activity_context": context.get("current_streamer_activity"),
            "room_context": {
                "danmaku_rate": context.get("danmaku_rate", 0),
                "audience_sentiment": PersonaEngine._average_recent_sentiment(context),
                "awaiting_reply_from_current_viewer": bool(
                    context.get("prompt_ram_awaiting_current_viewer")
                ),
            },
            "relationship_boundary": {"interaction_boundary": boundary},
        }

    @staticmethod
    def _average_recent_sentiment(memory_context: dict) -> float:
        values = []
        for item in memory_context.get("recent_danmaku", []) or []:
            try:
                values.append(float(item.get("sentiment", 0)))
            except (AttributeError, TypeError, ValueError):
                continue
        return max(-1.0, min(1.0, sum(values) / len(values))) if values else 0.0

    @staticmethod
    def _log_event_appraisal(analysis) -> None:
        """兼容旧扩展/测试注入的分析对象，缺失新字段时只降级日志。"""
        appraisal = getattr(analysis, "appraisal", None)
        if appraisal is None:
            logger.info("  事件评价: legacy_unavailable")
            return
        logger.info(
            "  事件评价: trigger=%s reward_threat=%+.2f affiliation=%+.2f pressure=%+.2f confidence=%.2f",
            appraisal.trigger_class.value,
            appraisal.reward_or_threat,
            appraisal.affiliation,
            appraisal.agency_or_pressure,
            appraisal.confidence,
        )
    
    async def _update_mood_with_ai_analysis(self, danmaku_message: str, memory_context: Optional[dict] = None):
        """
        使用AI分析弹幕对人格状态的影响并更新
        具备可解释性和边界控制
        返回: (analysis, mood_before, stress_before, darkness_before)
        """
        # 记录更新前的状态
        mood_before = self.state.mood
        stress_before = self.state.stress
        darkness_before = self.state.darkness
        
        # 使用人格影响分析器分析弹幕
        analysis = await persona_impact_analyzer.analyze_danmaku_impact(
            danmaku_message,
            self.state
        )
        
        if analysis:
            # 获取情绪变化对象
            delta = persona_impact_analyzer.get_emotion_delta(analysis)
            dynamics_context = persona_dynamics.build_context(
                memory_context=memory_context,
                danmaku_message=danmaku_message,
                source="reply_analysis"
            )
            
            internal_delta = internal_state_dynamics.derive_delta(
                self.internal_state, analysis, dynamics_context
            )
            await self._commit_semantic_impact(
                "legacy_analysis",
                delta,
                internal_delta,
                dynamics_context,
            )
            
            # 记录详细的可解释性日志
            logger.info(f"AI驱动的人格状态更新:")
            logger.info(f"  情感倾向: {analysis.emotional_tone}")
            logger.info(f"  内容强度: {analysis.content_intensity:.2f}")
            logger.info(f"  上下文相关性: {analysis.context_relevance:.2f}")
            logger.info(f"  关键因素: {', '.join(analysis.key_factors)}")
            self._log_event_appraisal(analysis)
            logger.info(f"  数值变化: mood={analysis.mood_impact:+.3f}, "
                      f"stress={analysis.stress_impact:+.3f}, "
                      f"darkness={analysis.darkness_impact:+.3f}")
            logger.info(f"  动力学后状态: mood={self.state.mood:.2f}, "
                      f"stress={self.state.stress:.2f}, "
                      f"darkness={self.state.darkness:.2f}")
        
        return analysis, mood_before, stress_before, darkness_before
    
    def _get_default_reply(self, danmaku_message: Dict[str, Any]) -> Dict[str, Any]:
        """获取默认回复"""
        nickname = danmaku_message.get('nickname', '小可爱')
        message = danmaku_message.get('message', '')
        
        # 根据消息内容返回不同的默认回复
        if any(keyword in message for keyword in ['好棒', '喜欢', '爱', '超棒']):
            return {
                "emotions": ["开心", "温柔"],
                "sentences": [
                    {"emotion": "开心", "text": f"谢谢{nickname}的喜欢💕"},
                    {"emotion": "温柔", "text": "你们的支持是我最大的动力✨"}
                ]
            }
        elif any(keyword in message for keyword in ['你好', '嗨', 'hello', 'hi']):
            return {
                "emotions": ["开心", "兴奋"],
                "sentences": [
                    {"emotion": "兴奋", "text": f"你好呀{nickname}💕"},
                    {"emotion": "开心", "text": "今天过得怎么样呀～"}
                ]
            }
        elif any(keyword in message for keyword in ['难过', '不开心', '伤心', '失望']):
            return {
                "emotions": ["温柔", "关心"],
                "sentences": [
                    {"emotion": "温柔", "text": f"怎么了{nickname}，抱抱你🫂"},
                    {"emotion": "关心", "text": "有什么烦心事可以跟我说一说呀～"}
                ]
            }
        else:
            return {
                "emotions": ["温柔", "开心"],
                "sentences": [
                    {"emotion": "温柔", "text": f"看到{nickname}的弹幕啦✨"},
                    {"emotion": "开心", "text": "大家的弹幕我都有在看哦～"}
                ]
            }


persona_engine = PersonaEngine()
persona_event_pipeline.bind(persona_engine.get_event_states, persona_engine.apply_event_mutation)
