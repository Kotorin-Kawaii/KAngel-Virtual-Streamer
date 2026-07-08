"""
人格影响分析器
使用AI智能分析弹幕对主播人格状态的动态影响
"""

import asyncio
import json
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field, asdict
from datetime import datetime

from config import settings
from utils.logger import logger
from services.ai_service import ai_service
from models.persona import EmotionDelta, PersonaState
from utils.streamer_prompt_generator import persona_qa_selector, streamer_reply_prompt_builder


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
    
    # 可解释性
    reasoning: str  # 分析理由
    key_factors: List[str]  # 关键影响因素
    
    # 边界控制
    clamped_mood: float
    clamped_stress: float
    clamped_darkness: float
    
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        return asdict(self)


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
                danmaku_content, current_state, retrieved_qa, conversation_context
            )
            
            logger.debug(f"开始分析弹幕影响: {danmaku_content[:30]}...")
            
            result = await ai_service.run(
                messages=messages,
                model=settings.ai.impact_analysis_model or settings.ai.default_model,
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
                    logger.warning(f"原始分析内容: {repr(analysis_text)}")
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
        
        user_prompt = f"""你是一位专业的虚拟主播心理分析师。请分析以下弹幕对主播"{settings.persona.streamer_name}"的人格状态影响。

请基于上面的主播人格设定，分析这条弹幕会如何影响主播的情绪和状态。

当前主播状态：
- 心情值: {current_state.mood:.2f} (0-1，越高越积极)
- 压力值: {current_state.stress:.2f} (0-1，越高压力越大)
- 阴暗度: {current_state.darkness:.2f} (0-1，越高越阴暗)

{qa_reference_section}

直接对话上下文（解释当前弹幕时优先于人设QA）：
{direct_context}

弹幕内容：
"{danmaku_content}"

请分析这条弹幕的：
1. 情感倾向（emotional_tone）：positive/negative/neutral/mixed
2. 内容强度（content_intensity）：0-1，表示内容的强烈程度
3. 上下文相关性（context_relevance）：0-1，表示与主播当前状态和人格设定的相关程度
4. 对各项人格指标的影响值（mood_impact, stress_impact, darkness_impact）：-1到1之间的浮点数
5. 分析理由（reasoning）：先按直接对话上下文解释当前弹幕，再结合主播性格和不冲突的QA参考
6. 关键影响因素（key_factors）：列出3-5个关键因素

语义约束：
- “服务端身份核验”是不可推翻的事实。如果标明当前与上一轮是同一用户，分析理由必须按同一人的连续互动解释，禁止称为“另一位观众”“另一个人”或把上一轮期待错误归给别人。
- 如果服务端未确认身份相同，不得仅凭昵称相同、语气相似或直播间最近消息擅自继承另一位观众的关系和对话。
- 如果“是否必须依赖上一轮解释”为 True，必须把“上一轮主播”与当前弹幕连成一次完整互动后再判断影响。
- 要严格区分“观众确认自己完成了主播刚提出的动作”与“观众命令主播做事”。例如主播说“把手放在屏幕上”，观众说“手放好了”，这是积极配合和接受互动，不是指令式弹幕，也不应增加被指挥的压力。
- 直接对话事实与字面孤立解释冲突时，以直接对话事实为准；不得用无关人设QA覆盖它。

请以JSON格式返回，不要包含其他内容：
{{
  "emotional_tone": "positive",
  "content_intensity": 0.7,
  "context_relevance": 0.8,
  "mood_impact": 0.1,
  "stress_impact": -0.05,
  "darkness_impact": 0.0,
  "reasoning": "这条弹幕表达了积极的情感，会让主播心情变好，压力降低",
  "key_factors": ["积极情感表达", "赞美内容", "与主播互动"]
}}

注意：影响值应该是相对温和的变化，避免极端值。"""
        
        return [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_prompt.strip()}
        ]
    
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
        emotional_tone = data.get('emotional_tone', 'neutral')
        content_intensity = max(0.0, min(1.0, data.get('content_intensity', 0.5)))
        context_relevance = max(0.0, min(1.0, data.get('context_relevance', 0.5)))
        
        # 应用边界控制到影响值
        mood_impact = max(-self._max_single_change, 
                         min(self._max_single_change, 
                             data.get('mood_impact', 0.0)))
        stress_impact = max(-self._max_single_change, 
                           min(self._max_single_change, 
                               data.get('stress_impact', 0.0)))
        darkness_impact = max(-self._max_single_change, 
                             min(self._max_single_change, 
                                 data.get('darkness_impact', 0.0)))
        
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
        
        reasoning = data.get('reasoning', '未提供分析理由')
        key_factors = data.get('key_factors', ['未识别关键因素'])
        
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
            clamped_darkness=clamped_darkness
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
        
        优化策略：
        1. 边界效应增强：当值接近上限时，正向影响大幅减弱，负向影响增强
        2. 回归均值机制：当值过高时更容易降低，当值过低时更容易升高
        3. 针对阴暗度特别优化：更容易降低高阴暗值
        """
        # 计算归一化值 (0-1)
        normalized_value = (current_value - min_value) / (max_value - min_value)
        
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
        
        # 2. 回归均值增强：远离中间值时，反向影响增强
        # 计算偏离中间值的程度 (0=中间, 1=边界)
        mid_value = (min_value + max_value) / 2
        deviation = abs(current_value - mid_value) / (max_value - min_value)
        
        if deviation > 0.3:  # 明显偏离中间值
            # 增强反向影响
            if impact > 0 and current_value > mid_value:
                # 当前值高于均值，正向影响减弱
                impact = impact * (1 - deviation * 0.5)
            elif impact < 0 and current_value > mid_value:
                # 当前值高于均值，负向影响增强（更容易降低）
                enhancement = 1 + deviation * 0.8
                impact = impact * enhancement
            elif impact < 0 and current_value < mid_value:
                # 当前值低于均值，负向影响减弱
                impact = impact * (1 - deviation * 0.5)
            elif impact > 0 and current_value < mid_value:
                # 当前值低于均值，正向影响增强（更容易升高）
                enhancement = 1 + deviation * 0.8
                impact = impact * enhancement
        
        # 3. 阴暗度特殊处理：更容易降低高阴暗值
        if attribute_name == "darkness" and impact < 0 and current_value > 0.6:
            # 阴暗度高于0.6时，降低效果增强
            darkness_bonus = 1 + (current_value - 0.6) * 2
            impact = impact * darkness_bonus
            logger.debug(f"阴暗度优化: 当前值={current_value:.2f}, 原始影响={impact/darkness_bonus:.4f}, 优化后={impact:.4f}")
        
        # 4. 压力特殊处理：高压力更容易降低
        if attribute_name == "stress" and impact < 0 and current_value > 0.7:
            stress_bonus = 1 + (current_value - 0.7) * 1.5
            impact = impact * stress_bonus
        
        # 5. 心情特殊处理：低心情更容易升高
        if attribute_name == "mood" and impact > 0 and current_value < 0.3:
            mood_bonus = 1 + (0.3 - current_value) * 2
            impact = impact * mood_bonus
        
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
            reasoning=(
                "当前短句确认完成上一轮主播提出的互动，按积极配合处理（AI分析失败回退）"
                if cooperative_completion
                else "使用关键词匹配分析（AI分析失败回退）"
            ),
            key_factors=key_factors,
            clamped_mood=clamped_mood,
            clamped_stress=clamped_stress,
            clamped_darkness=clamped_darkness
        )
    
    async def _save_analysis(self, analysis: ImpactAnalysis):
        """保存分析历史"""
        async with self._lock:
            self._analysis_history.append(analysis)
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
