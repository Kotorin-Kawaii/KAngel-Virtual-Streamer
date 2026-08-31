"""
人格影响分析器
使用AI智能分析弹幕对主播人格状态的动态影响
"""

import asyncio
import json
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime

from config import settings
from kangel.shared.logging import logger
from kangel.integrations.ai.service import ai_service
from ..domain.state import EmotionDelta, PersonaState
from ..domain.appraisal import (
    EventAppraisal, EventTriggerClass, event_appraisal_projector,
)
from kangel.integrations.ai.prompts import persona_qa_selector, streamer_reply_prompt_builder


@dataclass
class ImpactAnalysis:
    """弹幕影响分析结果"""
    danmaku_content: str
    current_mood: float
    current_stress: float
    current_darkness: float

    # 分析维度
    emotional_tone: str  # 情感倾向: positive, negative, neutral, mixed
    content_intensity: float  # 内容强度 0-1
    context_relevance: float  # 上下文相关性 0-1

    # 影响评估
    mood_impact: float
    stress_impact: float
    darkness_impact: float

    # 仅为旧扩展的输入兼容保留；不再进入日志、历史或持久化。
    reasoning: str
    key_factors: List[str]  # 关键影响因素

    # 边界控制
    clamped_mood: float
    clamped_stress: float
    clamped_darkness: float

    # P22 结构化事件评价；三轴变化仍由兼容投影链路产生。
    appraisal: EventAppraisal = field(default_factory=lambda: EventAppraisal(
        EventTriggerClass.NEUTRAL_INTERACTION, 0.0, 0.0, 0.0, 0.0, 0.0,
    ))
    appraisal_source: str = "legacy"

    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("danmaku_content", None)
        payload.pop("reasoning", None)
        payload["appraisal"] = self.appraisal.to_dict()
        return payload


class PersonaImpactAnalyzer:
    """人格影响分析器"""

    def __init__(self):
        self._analysis_history: List[ImpactAnalysis] = []
        self._max_history = 50
        self._lock = asyncio.Lock()

        # 边界控制参数
        self._max_single_change = 0.15  # 单次最大变化
        self._min_mood = 0.05  # 最低心情值
        self._max_mood = 0.95  # 最高心情值
        self._min_stress = 0.05
        self._max_stress = 0.95
        self._min_darkness = 0.0
        self._max_darkness = 0.9

        # 调试模式
        self._debug_mode = True

    def set_debug_mode(self, enabled: bool):
        """设置调试模式"""
        self._debug_mode = enabled
        logger.info(f"人格影响分析器调试模式: {'启用' if enabled else '禁用'}")

    async def analyze_danmaku_impact(
        self,
        danmaku_content: str,
        current_state: PersonaState,
        retrieved_qa: Optional[List[Dict]] = None,
        conversation_context: Optional[Dict] = None,
        activity_context: Optional[Dict] = None,
        room_context: Optional[Dict] = None,
        relationship_boundary: Optional[Dict] = None,
    ) -> Optional[ImpactAnalysis]:
        """
        分析弹幕对人格状态的影响
        使用AI进行智能分析
        """
        try:
            if retrieved_qa is None:
                retrieved_qa = await persona_qa_selector.select(
                    danmaku_content, current_state, top_k=3,
                    conversation_context=conversation_context,
                )
            messages = self._build_analysis_prompt(
                danmaku_content, current_state, retrieved_qa, conversation_context,
                activity_context=activity_context,
                room_context=room_context,
                relationship_boundary=relationship_boundary,
            )

            logger.debug("开始分析弹幕影响")

            result = await ai_service.run(
                messages=messages,
                role="impact_analysis",
                model=settings.ai.impact_analysis_model or settings.ai.default_model,
                model_mode="role_hint",
                temperature=0.3,
                timeout=settings.ai.impact_analysis_timeout,
            )

            analysis_text = result.get('reply', '')

            if analysis_text:
                # 清理JSON标记
                cleaned_text = self._clean_json_text(analysis_text)

                try:
                    analysis_data = json.loads(cleaned_text)
                    analysis = self._parse_analysis_result(
                        analysis_data,
                        danmaku_content,
                        current_state
                    )

                    # 保存分析历史
                    await self._save_analysis(analysis)

                    if self._debug_mode:
                        logger.info(f"弹幕影响分析完成: {json.dumps(analysis.to_dict(), ensure_ascii=False)}")

                    return analysis

                except json.JSONDecodeError as e:
                    logger.error(f"解析分析结果失败: {e}")
                    logger.warning("影响分析响应不是有效 JSON，使用安全回退")
                    # 使用回退分析
                    return await self._fallback_analysis(
                        danmaku_content, current_state, conversation_context
                    )

            return await self._fallback_analysis(
                danmaku_content, current_state, conversation_context
            )

        except Exception as e:
            logger.error(f"弹幕影响分析出错: {e}")
            return await self._fallback_analysis(
                danmaku_content, current_state, conversation_context
            )

    def _build_analysis_prompt(
        self,
        danmaku_content: str,
        current_state: PersonaState,
        retrieved_qa: Optional[List[Dict]] = None,
        conversation_context: Optional[Dict] = None,
        activity_context: Optional[Dict] = None,
        room_context: Optional[Dict] = None,
        relationship_boundary: Optional[Dict] = None,
    ) -> List[Dict[str, str]]:
        """构建分析提示词，包含主播的完整人格设定和RAG检索的QA参考"""

        # 获取系统提示词
        system_prompt = streamer_reply_prompt_builder._build_system_prompt()

        qa_reference = streamer_reply_prompt_builder._format_retrieved_qa(retrieved_qa or [])
        qa_reference_section = (
            f"【相关人设QA参考】\n{qa_reference}\n" if qa_reference else ""
        )
        direct_context = persona_qa_selector._format_conversation_context(
            conversation_context
        )
        scene_context = self._format_minimal_scene_context(
            activity_context=activity_context,
            room_context=room_context,
            relationship_boundary=relationship_boundary,
        )

        if settings.ai.event_appraisal_enabled:
            impact_fields_instruction = """4. 事件评价（event_appraisal）：只输出受限字段，不要输出推理过程。`trigger_class` 只能是 affirmation/cooperative_response/distress_share/pressure_or_demand/boundary_challenge/activity_progress/neutral_interaction；reward_or_threat、affiliation、agency_or_pressure、novelty 取 -1 到 1；confidence 取 0 到 1
5. 关键影响类别（key_factors）：至多 3 个，只能从 affirmation/affiliation/cooperation/distress/pressure/boundary/activity/novelty/neutral 中选择"""
            impact_fields_schema = """  "event_appraisal": {
    "trigger_class": "affirmation",
    "reward_or_threat": 0.7,
    "affiliation": 0.6,
    "agency_or_pressure": 0.1,
    "novelty": 0.2,
    "confidence": 0.8
  },
  "key_factors": ["affirmation", "affiliation"]"""
        else:
            impact_fields_instruction = """4. 对各项人格指标的影响值（mood_impact, stress_impact, darkness_impact）：-1到1之间的浮点数
5. 关键影响类别（key_factors）：列出至多 3 个短类别，不要复述弹幕"""
            impact_fields_schema = """  "mood_impact": 0.1,
  "stress_impact": -0.05,
  "darkness_impact": 0.0,
  "key_factors": ["affirmation", "affiliation"]"""

        user_prompt = f"""你是一位专业的虚拟主播心理分析师。请分析以下弹幕对主播"{settings.persona.streamer_name}"的人格状态影响。

请基于上面的主播人格设定，分析这条弹幕会如何影响主播的情绪和状态。

当前主播状态：
- 心情值: {current_state.mood:.2f} (0-1，越高越积极)
- 压力值: {current_state.stress:.2f} (0-1，越高压力越大)
- 阴暗度: {current_state.darkness:.2f} (0-1，越高越阴暗)

{qa_reference_section}

直接对话上下文（解释当前弹幕时优先于人设QA）：
{direct_context}

已验证的最小场景事实（仅辅助判断，不能覆盖当前弹幕直接语义）：
{scene_context}

弹幕内容：
"{danmaku_content}"

请分析这条弹幕的：
1. 情感倾向（emotional_tone）：positive/negative/neutral/mixed
2. 内容强度（content_intensity）：0-1，表示内容的强烈程度
3. 上下文相关性（context_relevance）：0-1，表示与主播当前状态和人格设定的相关程度
{impact_fields_instruction}

语义约束：
- “服务端身份核验”是不可推翻的事实。如果标明当前与上一轮是同一用户，事件评价必须按同一人的连续互动解释，禁止称为“另一位观众”“另一个人”或把上一轮期待错误归给别人。
- 如果服务端未确认身份相同，不得仅凭昵称相同、语气相似或直播间最近消息擅自继承另一位观众的关系和对话。
- 如果“是否必须依赖上一轮解释”为 True，必须把“上一轮主播”与当前弹幕连成一次完整互动后再判断影响。
- 要严格区分“观众确认自己完成了主播刚提出的动作”与“观众命令主播做事”。例如主播说“把手放在屏幕上”，观众说“手放好了”，这是积极配合和接受互动，不是指令式弹幕，也不应增加被指挥的压力。
- 直接对话事实与字面孤立解释冲突时，以直接对话事实为准；不得用无关人设QA覆盖它。
- 如果“未闭合期待”标明本条正是主播在等的回话，应按“期待被满足”解释，配合与认可的增益更明显；但这只是语境，不改变字面语义的判断。

请以JSON格式返回，不要包含其他内容：
{{
  "emotional_tone": "positive",
  "content_intensity": 0.7,
  "context_relevance": 0.8,
{impact_fields_schema}
}}

注意：影响值应该是相对温和的变化，避免极端值。"""

        return [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_prompt.strip()}
        ]

    @staticmethod
    def _format_minimal_scene_context(
        *, activity_context: Optional[Dict], room_context: Optional[Dict],
        relationship_boundary: Optional[Dict],
    ) -> str:
        """只让分析器看到可验证的低密度事实，不透传个人记忆或原文。"""
        activity = activity_context or {}
        room = room_context or {}
        relationship = relationship_boundary or {}
        activity_text = "无已确认活动"
        if activity:
            category = str(activity.get("category", ""))[:24]
            display = str(activity.get("display_name", ""))[:80]
            object_name = str(activity.get("object_name", ""))[:80]
            activity_text = " / ".join(part for part in (category, display, object_name) if part) or activity_text
        try:
            danmaku_rate = max(0, min(999, int(room.get("danmaku_rate", 0))))
        except (TypeError, ValueError):
            danmaku_rate = 0
        try:
            audience_sentiment = max(-1.0, min(1.0, float(room.get("audience_sentiment", 0))))
        except (TypeError, ValueError):
            audience_sentiment = 0.0
        boundary = str(relationship.get("interaction_boundary", "standard"))[:24]
        # P30：工作记忆这一路只透传一个服务端可验证的布尔量，不带任何念头原文。
        awaiting = (
            "本条弹幕正是主播在等的回话"
            if room.get("awaiting_reply_from_current_viewer")
            else "无"
        )
        return (
            f"- 当前活动: {activity_text}\n"
            f"- 房间聚合: 弹幕速率={danmaku_rate}/分钟, 氛围倾向={audience_sentiment:+.2f}\n"
            f"- 未闭合期待: {awaiting}\n"
            f"- 已验证关系边界: {boundary}"
        )

    def _clean_json_text(self, text: str) -> str:
        """清理JSON文本"""
        cleaned = text.strip()

        # 移除 ```json 开头
        if cleaned.startswith('```json'):
            cleaned = cleaned[7:]
        elif cleaned.startswith('```'):
            cleaned = cleaned[3:]

        # 移除结尾的 ```
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]

        return cleaned.strip()

    def _parse_analysis_result(
        self,
        data: dict,
        danmaku_content: str,
        current_state: PersonaState
    ) -> ImpactAnalysis:
        """解析AI分析结果"""

        # 提取并验证各项值
        emotional_tone = str(data.get('emotional_tone', 'neutral'))
        if emotional_tone not in {'positive', 'negative', 'neutral', 'mixed'}:
            emotional_tone = 'neutral'
        content_intensity = self._bounded_float(data.get('content_intensity'), 0.5, 0.0, 1.0)
        context_relevance = self._bounded_float(data.get('context_relevance'), 0.5, 0.0, 1.0)

        # 旧响应仍可使用既有增量；含有效结构化评价的新响应由后端投影，
        # 不接受模型直接指定三轴结果。
        raw_mood_impact = self._bounded_float(
            data.get('mood_impact'), 0.0, -self._max_single_change, self._max_single_change
        )
        raw_stress_impact = self._bounded_float(
            data.get('stress_impact'), 0.0, -self._max_single_change, self._max_single_change
        )
        raw_darkness_impact = self._bounded_float(
            data.get('darkness_impact'), 0.0, -self._max_single_change, self._max_single_change
        )
        fallback_appraisal = self._legacy_appraisal(
            emotional_tone=emotional_tone,
            mood_impact=raw_mood_impact,
            stress_impact=raw_stress_impact,
            darkness_impact=raw_darkness_impact,
            intensity=content_intensity,
        )
        parsed_appraisal = (
            EventAppraisal.parse(data.get('event_appraisal'))
            if settings.ai.event_appraisal_enabled else None
        )
        appraisal = parsed_appraisal or fallback_appraisal
        appraisal_source = "model" if parsed_appraisal is not None else "legacy"
        if parsed_appraisal is not None:
            projected = event_appraisal_projector.project(parsed_appraisal)
            mood_impact, stress_impact, darkness_impact = (
                projected.mood, projected.stress, projected.darkness,
            )
        else:
            mood_impact, stress_impact, darkness_impact = (
                raw_mood_impact, raw_stress_impact, raw_darkness_impact,
            )

        # 根据当前状态动态调整影响权重（传入属性名称以便特殊处理）
        mood_impact = self._adjust_impact_by_current_state(
            mood_impact, current_state.mood, self._min_mood, self._max_mood, "mood"
        )
        stress_impact = self._adjust_impact_by_current_state(
            stress_impact, current_state.stress, self._min_stress, self._max_stress, "stress"
        )
        darkness_impact = self._adjust_impact_by_current_state(
            darkness_impact, current_state.darkness, self._min_darkness, self._max_darkness, "darkness"
        )

        # 计算边界控制后的新值
        clamped_mood = max(self._min_mood,
                          min(self._max_mood,
                              current_state.mood + mood_impact))
        clamped_stress = max(self._min_stress,
                            min(self._max_stress,
                                current_state.stress + stress_impact))
        clamped_darkness = max(self._min_darkness,
                              min(self._max_darkness,
                                  current_state.darkness + darkness_impact))

        # 兼容仍会返回 reasoning 的旧模型，但绝不采纳、记录或持久化该自由文本。
        reasoning = ""
        key_factors = (
            self._canonical_key_factors(data.get('key_factors'), appraisal)
            if settings.ai.event_appraisal_enabled
            else self._bounded_key_factors(data.get('key_factors'))
        )

        return ImpactAnalysis(
            danmaku_content=danmaku_content,
            current_mood=current_state.mood,
            current_stress=current_state.stress,
            current_darkness=current_state.darkness,
            emotional_tone=emotional_tone,
            content_intensity=content_intensity,
            context_relevance=context_relevance,
            mood_impact=mood_impact,
            stress_impact=stress_impact,
            darkness_impact=darkness_impact,
            reasoning=reasoning,
            key_factors=key_factors,
            clamped_mood=clamped_mood,
            clamped_stress=clamped_stress,
            clamped_darkness=clamped_darkness,
            appraisal=appraisal,
            appraisal_source=appraisal_source,
        )

    @staticmethod
    def _bounded_float(value: Any, default: float, lower: float, upper: float) -> float:
        try:
            return max(lower, min(upper, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_key_factors(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip()[:40] for item in value[:3] if str(item).strip()]

    @staticmethod
    def _canonical_key_factors(value: Any, appraisal: EventAppraisal) -> List[str]:
        """key_factors 只供日志/debug；归一为有限诊断 taxonomy。"""
        aliases = {
            "affirmation": "affirmation", "support": "affirmation", "praise": "affirmation",
            "affiliation": "affiliation", "social_affiliation": "affiliation",
            "continued_affiliation": "affiliation", "mild_affiliation": "affiliation",
            "familiar_affiliation": "affiliation", "cooperative_response": "cooperation",
            "cooperation": "cooperation", "distress_share": "distress",
            "distress": "distress", "pressure_or_demand": "pressure",
            "pressure": "pressure", "boundary_challenge": "boundary",
            "boundary": "boundary", "activity_progress": "activity",
            "activity": "activity", "novelty": "novelty",
            "neutral_interaction": "neutral", "neutral": "neutral",
        }
        factors: List[str] = []
        if isinstance(value, list):
            for raw in value:
                canonical = aliases.get(str(raw).strip().casefold().replace("-", "_"))
                if canonical and canonical not in factors:
                    factors.append(canonical)
                if len(factors) == 3:
                    break
        if not factors:
            factors.append(aliases.get(appraisal.trigger_class.value, "neutral"))
        return factors

    @staticmethod
    def _legacy_appraisal(
        *, emotional_tone: str, mood_impact: float, stress_impact: float,
        darkness_impact: float, intensity: float,
    ) -> EventAppraisal:
        """旧模型响应安全降级为可解释结构，不把旧 reasoning 当事实。"""
        if emotional_tone == 'positive':
            trigger = EventTriggerClass.AFFIRMATION
        elif emotional_tone == 'negative':
            trigger = EventTriggerClass.DISTRESS_SHARE
        else:
            trigger = EventTriggerClass.NEUTRAL_INTERACTION
        return EventAppraisal(
            trigger_class=trigger,
            reward_or_threat=max(-1.0, min(1.0, (mood_impact - stress_impact) * 4)),
            affiliation=max(-1.0, min(1.0, mood_impact * 5)),
            agency_or_pressure=max(-1.0, min(1.0, -stress_impact * 5)),
            novelty=max(-1.0, min(1.0, intensity * 2 - 1)),
            confidence=0.35,
        )

    def _adjust_impact_by_current_state(
        self,
        impact: float,
        current_value: float,
        min_value: float,
        max_value: float,
        attribute_name: str = "unknown"
    ) -> float:
        """
        根据当前状态动态调整影响值

        约束策略：
        1. 接近物理边界时，同向影响衰减
        2. 仅在极端边界附近提供很弱的安全回归，不把中度低落/高压自动拉回乐观值
        """
        # 计算距离边界的距离
        distance_to_max = max_value - current_value
        distance_to_min = current_value - min_value

        # 1. 边界衰减：接近边界时，同向影响减弱
        if impact > 0:
            # 正向影响：距离上限越近，影响越小
            if distance_to_max < 0.3:
                # 接近上限时，影响指数衰减
                decay_factor = (distance_to_max / 0.3) ** 2  # 平方衰减，更明显
                impact = impact * decay_factor
        elif impact < 0:
            # 负向影响：距离下限越近，影响越小
            if distance_to_min < 0.3:
                decay_factor = (distance_to_min / 0.3) ** 2
                impact = impact * decay_factor

        # 2. 回归均值仅作为接近极端边界时的安全保护。
        # 计算偏离中间值的程度 (0=中间, 1=边界)
        mid_value = (min_value + max_value) / 2
        deviation = abs(current_value - mid_value) / (max_value - min_value)
        tuning = settings.persona.dynamics
        if deviation > tuning.mean_reversion_threshold:
            if impact > 0 and current_value > mid_value:
                impact = impact * max(0.65, 1 - deviation * tuning.mean_reversion_strength)
            elif impact < 0 and current_value > mid_value:
                enhancement = 1 + deviation * tuning.mean_reversion_strength
                impact = impact * enhancement
            elif impact < 0 and current_value < mid_value:
                impact = impact * max(0.65, 1 - deviation * tuning.mean_reversion_strength)
            elif impact > 0 and current_value < mid_value:
                enhancement = 1 + deviation * tuning.mean_reversion_strength
                impact = impact * enhancement

        # 3. 轴特定保护也仅在接近边界时生效，避免形成默认“乐观复位”。
        multiplier = tuning.extreme_guard_recovery_multiplier
        if attribute_name == "darkness" and impact < 0 and current_value >= tuning.extreme_guard_darkness_ceiling:
            impact *= multiplier
        elif attribute_name == "stress" and impact < 0 and current_value >= tuning.extreme_guard_stress_ceiling:
            impact *= multiplier
        elif attribute_name == "mood" and impact > 0 and current_value <= tuning.extreme_guard_mood_floor:
            impact *= multiplier

        return impact

    async def _fallback_analysis(
        self,
        danmaku_content: str,
        current_state: PersonaState,
        conversation_context: Optional[Dict] = None,
    ) -> ImpactAnalysis:
        """
        回退分析方法：当AI分析失败时使用
        使用关键词匹配进行基础分析
        """
        logger.warning("使用回退分析方法")

        danmaku_lower = danmaku_content.lower()

        # 关键词匹配
        positive_keywords = ['好棒', '喜欢', '爱', '超棒', '可爱', '加油', '支持', '好听', '厉害', '优秀', '棒']
        negative_keywords = ['不好', '讨厌', '失望', '难过', '伤心', '生气', '无聊', '差', '烂', '垃圾']
        dark_keywords = ['黑暗', '痛苦', '绝望', '孤独', '死亡', '意义', '虚无', '自杀', '抑郁', '焦虑']

        positive_count = sum(1 for kw in positive_keywords if kw in danmaku_lower)
        negative_count = sum(1 for kw in negative_keywords if kw in danmaku_lower)
        dark_count = sum(1 for kw in dark_keywords if kw in danmaku_lower)

        # 计算基础影响
        mood_impact = (positive_count * 0.03) - (negative_count * 0.03)
        stress_impact = -(positive_count * 0.02) + (negative_count * 0.02)
        darkness_impact = dark_count * 0.02

        cooperative_completion = bool(
            conversation_context
            and conversation_context.get("depends_on_previous")
            and "确认已完成" in str(
                conversation_context.get("resolved_reference", "")
            )
        )
        if cooperative_completion:
            mood_impact += 0.03
            stress_impact -= 0.03

        # 确定情感倾向
        if cooperative_completion:
            emotional_tone = "positive"
        elif positive_count > negative_count:
            emotional_tone = "positive"
        elif negative_count > positive_count:
            emotional_tone = "negative"
        else:
            emotional_tone = "neutral"

        # 内容强度
        content_intensity = min((positive_count + negative_count + dark_count) * 0.1, 1.0)
        if cooperative_completion:
            content_intensity = max(content_intensity, 0.3)

        # 应用边界控制
        mood_impact = max(-self._max_single_change, min(self._max_single_change, mood_impact))
        stress_impact = max(-self._max_single_change, min(self._max_single_change, stress_impact))
        darkness_impact = max(-self._max_single_change, min(self._max_single_change, darkness_impact))

        # 动态调整（传入属性名称以便特殊处理）
        mood_impact = self._adjust_impact_by_current_state(
            mood_impact, current_state.mood, self._min_mood, self._max_mood, "mood"
        )
        stress_impact = self._adjust_impact_by_current_state(
            stress_impact, current_state.stress, self._min_stress, self._max_stress, "stress"
        )
        darkness_impact = self._adjust_impact_by_current_state(
            darkness_impact, current_state.darkness, self._min_darkness, self._max_darkness, "darkness"
        )

        clamped_mood = max(self._min_mood, min(self._max_mood, current_state.mood + mood_impact))
        clamped_stress = max(self._min_stress, min(self._max_stress, current_state.stress + stress_impact))
        clamped_darkness = max(self._min_darkness, min(self._max_darkness, current_state.darkness + darkness_impact))

        key_factors = []
        if positive_count > 0:
            key_factors.append("积极关键词")
        if negative_count > 0:
            key_factors.append("消极关键词")
        if dark_count > 0:
            key_factors.append("阴暗话题关键词")
        if not key_factors:
            key_factors.append("中性内容")
        if cooperative_completion:
            key_factors = ["承接上一轮主播互动", "观众积极配合"] + [
                factor for factor in key_factors if factor != "中性内容"
            ]

        return ImpactAnalysis(
            danmaku_content=danmaku_content,
            current_mood=current_state.mood,
            current_stress=current_state.stress,
            current_darkness=current_state.darkness,
            emotional_tone=emotional_tone,
            content_intensity=content_intensity,
            context_relevance=0.9 if cooperative_completion else 0.5,
            mood_impact=mood_impact,
            stress_impact=stress_impact,
            darkness_impact=darkness_impact,
            reasoning="",
            key_factors=key_factors,
            clamped_mood=clamped_mood,
            clamped_stress=clamped_stress,
            clamped_darkness=clamped_darkness,
            appraisal=EventAppraisal(
                trigger_class=(
                    EventTriggerClass.COOPERATIVE_RESPONSE if cooperative_completion
                    else EventTriggerClass.AFFIRMATION if positive_count > negative_count
                    else EventTriggerClass.DISTRESS_SHARE if negative_count > positive_count
                    else EventTriggerClass.NEUTRAL_INTERACTION
                ),
                reward_or_threat=max(-1.0, min(1.0, (mood_impact - stress_impact) * 4)),
                affiliation=max(-1.0, min(1.0, mood_impact * 5)),
                agency_or_pressure=max(-1.0, min(1.0, -stress_impact * 5)),
                novelty=max(-1.0, min(1.0, content_intensity * 2 - 1)),
                confidence=0.45 if cooperative_completion else 0.3,
            ),
            appraisal_source="fallback",
        )

    async def _save_analysis(self, analysis: ImpactAnalysis):
        """只保留受限结构化诊断；不留弹幕原文或模型自由文本。"""
        async with self._lock:
            self._analysis_history.append(replace(
                analysis, danmaku_content="", reasoning="", key_factors=analysis.key_factors[:3],
            ))
            if len(self._analysis_history) > self._max_history:
                self._analysis_history.pop(0)

    def get_analysis_history(self, limit: int = 10) -> List[dict]:
        """获取分析历史"""
        return [a.to_dict() for a in self._analysis_history[-limit:]]

    def get_emotion_delta(self, analysis: ImpactAnalysis) -> EmotionDelta:
        """从分析结果获取情绪变化对象"""
        return EmotionDelta(
            mood=analysis.mood_impact,
            stress=analysis.stress_impact,
            darkness=analysis.darkness_impact
        )

    def apply_analysis_to_state(self, analysis: ImpactAnalysis, current_state: PersonaState) -> PersonaState:
        """应用分析结果到人格状态"""
        return PersonaState(
            mood=analysis.clamped_mood,
            stress=analysis.clamped_stress,
            darkness=analysis.clamped_darkness
        )

    def get_debug_info(self) -> dict:
        """获取调试信息"""
        return {
            "debug_mode": self._debug_mode,
            "analysis_count": len(self._analysis_history),
            "max_history": self._max_history,
            "max_single_change": self._max_single_change,
            "boundaries": {
                "mood": {"min": self._min_mood, "max": self._max_mood},
                "stress": {"min": self._min_stress, "max": self._max_stress},
                "darkness": {"min": self._min_darkness, "max": self._max_darkness}
            }
        }


# 全局人格影响分析器实例
persona_impact_analyzer = PersonaImpactAnalyzer()
