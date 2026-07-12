import asyncio
from time import perf_counter
from functools import wraps
from typing import Optional, List, Dict, Any
from ..domain.state import (
    PersonaState, PersonaBehavior, PersonaDecision, EmotionDelta,
    InternalPersonaState, InternalStateDelta
)
from config import settings
from kangel.integrations.ai.service import ai_service
from kangel.integrations.ai.prompts import (
    persona_decision_prompt_builder,
    persona_qa_selector,
    streamer_reply_prompt_builder,
)
from kangel.shared.logging import logger
from .impact_analyzer import persona_impact_analyzer
from ..domain.dynamics import DynamicsContext
from kangel.audience.application.relationship_service import audience_relationship_manager
from ..domain.events import SemanticImpactAnalyzedEvent
from ..domain.mutations import PersonaMutation
from .runtime import internal_state_dynamics, persona_dynamics, persona_event_pipeline
from kangel.danmaku.application.memory import danmaku_memory_manager
from kangel.infrastructure.database import db_manager
from kangel.danmaku.application.pool import danmaku_pool
from .emotion_manager import emotion_manager
from kangel.audience.application.nickname_history import nickname_history_context_manager
from kangel.memory.application.long_term_memory import ConversationContinuityAnalyzer, long_term_memory_manager
from kangel.stream.application.metadata import stream_metadata_pusher
from ..infrastructure.state_repository import (
    DatabasePersonaStateRepository,
    PersonaStateRepository,
)


_damaku_lock = asyncio.Lock()
_conversation_continuity = ConversationContinuityAnalyzer()


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
            logger.info(f"🚀 人格引擎启动成功，从数据库加载状态: mood={self.state.mood:.2f}, stress={self.state.stress:.2f}, darkness={self.state.darkness:.2f}")
        else:
            # 使用默认状态
            self.state = PersonaState(
                mood=settings.persona.initial_mood,
                darkness=settings.persona.initial_darkness,
                stress=settings.persona.initial_stress
            )
            logger.info(f"🚀 人格引擎启动成功，使用默认状态: mood={self.state.mood:.2f}, stress={self.state.stress:.2f}, darkness={self.state.darkness:.2f}")

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
        if dynamics_context:
            delta = persona_dynamics.apply(self.state, delta, dynamics_context)

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
    ) -> None:
        await persona_event_pipeline.publish(
            SemanticImpactAnalyzedEvent(
                danmaku_id=danmaku_id,
                raw_delta=raw_delta,
                internal_delta=internal_delta,
                dynamics_context=dynamics_context,
                source="persona_impact_analyzer",
                source_event_id=f"danmaku:{danmaku_id}",
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
                model=settings.ai.danmaku_selector_model or settings.ai.default_model,
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
            memory_context["danmaku_rate"] = persona_event_pipeline.current_danmaku_rate
            memory_context["daily_stream_theme"] = (
                stream_metadata_pusher.get_theme_prompt_context()
            )
            memory_context["current_streamer_activity"] = (
                stream_metadata_pusher.get_activity_prompt_context()
            )
            conversation_context = self._build_direct_conversation_context(
                long_term_context=long_term_memory_context,
                replied_danmaku=replied_danmaku,
                identity=danmaku_message.get('_viewer_identity'),
                current_message=danmaku_message.get('message', ''),
                nickname=danmaku_message.get('nickname', ''),
            )

            # QA 只为最终回复提供人设证据；情感分析使用完整人格、当前状态与
            # 已核验的直接上下文，两者不再互相等待。
            if conversation_context and conversation_context.get("depends_on_previous"):
                retrieved_qa = []
                logger.info(
                    "当前弹幕依赖上一轮直接语义，本轮跳过人设QA检索: %r",
                    danmaku_message.get('message', '')[:40],
                )
                analysis = await persona_impact_analyzer.analyze_danmaku_impact(
                    danmaku_message.get('message', ''),
                    self.state,
                    retrieved_qa=[],
                    conversation_context=conversation_context,
                )
            elif settings.ai.parallel_context_analysis:
                parallel_started = perf_counter()
                retrieved_qa, analysis = await asyncio.gather(
                    persona_qa_selector.select(
                        danmaku_message.get('message', ''),
                        self.state,
                        top_k=3,
                        conversation_context=conversation_context,
                    ),
                    persona_impact_analyzer.analyze_danmaku_impact(
                        danmaku_message.get('message', ''),
                        self.state,
                        retrieved_qa=[],
                        conversation_context=conversation_context,
                    ),
                )
                logger.info(
                    "QA选择与情感影响并行完成，关键路径耗时=%dms",
                    round((perf_counter() - parallel_started) * 1000),
                )
            else:
                retrieved_qa = await persona_qa_selector.select(
                    danmaku_message.get('message', ''),
                    self.state,
                    top_k=3,
                    conversation_context=conversation_context,
                )
                analysis = await persona_impact_analyzer.analyze_danmaku_impact(
                    danmaku_message.get('message', ''),
                    self.state,
                    retrieved_qa=[],
                    conversation_context=conversation_context,
                )

            # 在生成回复前只分析一次当前弹幕，用投影状态指导本轮即时反应。
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

            current_persona_state = reaction_state.model_dump()
            emotion_context = emotion_manager.build_prompt_context(
                reaction_state.mood,
                reaction_state.stress,
                reaction_state.darkness,
            )
            logger.debug(f"[DEBUG] 本轮即时反应状态: {current_persona_state}")
            
            logger.debug(f"[DEBUG] 开始生成提示词...")
            messages, response_format = streamer_reply_prompt_builder.generate_prompt(
                additional_context=danmaku_message.get('message', ''),
                persona_state=current_persona_state,
                memory_context=memory_context,
                internal_state=reaction_internal_state.model_dump(),
                emotion_context=emotion_context,
                retrieved_qa=retrieved_qa,
                conversation_context=conversation_context,
                is_sc_danmaku=bool(danmaku_message.get("_is_sc_danmaku")),
            )
            logger.debug(f"[DEBUG] 提示词生成完成，消息数量: {len(messages)}")
            
            logger.info(f"开始生成回复，弹幕: {danmaku_message.get('message', '')[:30]}...")
            
            result = await self.ai_service.run(
                messages=messages,
                model=settings.ai.default_model,
                response_format=response_format
            )
            
            reply_text = result.get('reply', '')
            logger.debug(f"AI返回内容: {reply_text[:100]}...")
            
            reply_data = None
            reply_generated = False
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
            
            # 回复确定后提交同一次分析产生的长期变化，避免重复计算。
            if reply_data and raw_delta and dynamics_context:
                state_commit_started = True
                internal_commit_started = internal_delta is not None
                await self._commit_semantic_impact(
                    danmaku_message.get('danmakuID', ''),
                    raw_delta,
                    internal_delta or InternalStateDelta(),
                    dynamics_context,
                )
                self._log_impact_analysis(danmaku_message.get('message', ''), analysis)
            if reply_data:
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
            if reply_data:
                emotion_manager.record_emotions(reply_data.get("emotions", []))
                emotion_history_recorded = True
            
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
                )
                self._log_impact_analysis(danmaku_message.get('message', ''), analysis)
            if analysis and dynamics_context and internal_delta is None:
                internal_delta = internal_state_dynamics.derive_delta(
                    self.internal_state, analysis, dynamics_context
                )
            if internal_delta and not internal_commit_started:
                internal_commit_started = True
                self.update_internal_state(internal_delta)
            if not relationship_reply_recorded:
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
        logger.info(f"  弹幕: {danmaku_message[:30]}...")
        logger.info(f"  情感倾向: {analysis.emotional_tone}")
        logger.info(f"  内容强度: {analysis.content_intensity:.2f}")
        logger.info(f"  上下文相关性: {analysis.context_relevance:.2f}")
        logger.info(f"  关键因素: {', '.join(analysis.key_factors)}")
        logger.info(f"  分析理由: {analysis.reasoning}")
        logger.info(
            f"  动力学后状态: mood={self.state.mood:.2f}, "
            f"stress={self.state.stress:.2f}, darkness={self.state.darkness:.2f}"
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
            logger.info(f"  弹幕: {danmaku_message[:30]}...")
            logger.info(f"  情感倾向: {analysis.emotional_tone}")
            logger.info(f"  内容强度: {analysis.content_intensity:.2f}")
            logger.info(f"  上下文相关性: {analysis.context_relevance:.2f}")
            logger.info(f"  关键因素: {', '.join(analysis.key_factors)}")
            logger.info(f"  分析理由: {analysis.reasoning}")
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
