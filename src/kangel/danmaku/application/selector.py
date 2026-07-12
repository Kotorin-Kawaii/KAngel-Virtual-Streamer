"""
弹幕选择器模块
结合AI人格和当前心情状态选择最合适的弹幕进行回复
"""

import asyncio
import json
import random
import time
from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass

from config import settings
from kangel.shared.logging import logger
from .pool import DanmakuPool, DanmakuItem, danmaku_pool
from kangel.persona.application.engine import PersonaEngine, persona_engine
from .load_tracker import resolve_danmaku_load
from kangel.infrastructure.rate_limiter import concurrency_gate
from kangel.integrations.ai.service import ai_service


@dataclass
class SelectionResult:
    """选择结果"""
    selected_danmaku: Optional[DanmakuItem]
    selection_reason: str
    confidence_score: float
    processing_time_ms: float


class DanmakuSelector:
    """弹幕选择器"""
    
    def __init__(self, *, clock=None, random_value=None):
        self._lock = asyncio.Lock()
        self._clock = clock or time.monotonic
        self._random_value = random_value or random.random
        self._last_selection_at: Optional[float] = None
        self._last_selection_time: Optional[datetime] = None
        self._selection_count = 0
        self._load_profile = resolve_danmaku_load(
            0, settings.danmaku.frequency_threshold
        )
        # 安全地获取权重配置，避免类型检查警告
        self._weights = getattr(settings.danmaku, "selector_weights", {
            "content_relevance": 0.3,
            "sender_level": 0.2,
            "emotional_match": 0.25,
            "timeliness": 0.15,
            "persona_consistency": 0.1
        })
    
    async def should_select_danmaku(self, current_rate: int, has_available_danmaku: bool = False) -> bool:
        """
        判断是否应该触发弹幕选择
        每次收到新弹幕都会检查，确保有弹幕就尝试回复
        """
        self._load_profile = resolve_danmaku_load(
            current_rate, settings.danmaku.frequency_threshold
        )
        now = self._clock()
        if self._last_selection_at is not None:
            time_since_last = max(0.0, now - self._last_selection_at)
            if time_since_last < self._load_profile.min_selection_interval_seconds:
                return False
        
        # 如果有可用弹幕，就尝试选择
        if has_available_danmaku:
            # 如果是第一次选择，直接触发
            if self._last_selection_at is None:
                logger.debug("首次选择，直接触发")
                return True

            time_since_last = max(0.0, now - self._last_selection_at)
            if time_since_last >= self._load_profile.force_selection_after_seconds:
                logger.debug(
                    "达到%s负载强制响应窗口，触发弹幕选择",
                    self._load_profile.level,
                )
                return True

            trigger_probability = min(
                persona_engine.behavior.reply_aggressiveness
                * 2
                * self._load_profile.trigger_probability_multiplier,
                1.0,
            )
            should_trigger = self._random_value() < trigger_probability
            if should_trigger:
                logger.debug(
                    "随机触发弹幕选择（负载: %s, 概率: %.2f）",
                    self._load_profile.level,
                    trigger_probability,
                )
            return should_trigger
        
        return False

    def get_load_snapshot(self) -> Dict[str, Any]:
        """供后续调度器与状态接口复用当前负载策略。"""
        return self._load_profile.to_dict()
    
    async def select_danmaku(self, available_danmaku: List[DanmakuItem]) -> Optional[SelectionResult]:
        """
        从可用弹幕中选择最合适的一条
        使用AI结合当前心情状态进行选择
        """
        if not available_danmaku:
            return None
        
        start_time = datetime.now()
        scored_danmaku = []
        
        try:
            # 第一步：计算每条弹幕的基础评分
            scored_danmaku = await self._calculate_base_scores(available_danmaku)
            
            # 第二步：使用AI进行智能选择
            selected = await self._ai_select_danmaku(scored_danmaku)
            
            processing_time = (datetime.now() - start_time).total_seconds() * 1000
            
            if selected:
                self._last_selection_at = self._clock()
                self._last_selection_time = datetime.now()
                self._selection_count += 1
                
                # 标记为已选中
                selected.status = DanmakuItem.__dataclass_fields__['status'].type.SELECTED
                
                return SelectionResult(
                    selected_danmaku=selected,
                    selection_reason="AI智能选择",
                    confidence_score=selected.priority,
                    processing_time_ms=processing_time
                )
            
            return None
            
        except Exception as e:
            logger.error(f"弹幕选择过程出错: {e}")
            processing_time = (datetime.now() - start_time).total_seconds() * 1000
            
            # 出错时选择优先级最高的一条
            if scored_danmaku:
                best = max(scored_danmaku, key=lambda x: x.priority)
                return SelectionResult(
                    selected_danmaku=best,
                    selection_reason="出错回退：选择最高优先级",
                    confidence_score=best.priority,
                    processing_time_ms=processing_time
                )
            
            return None
    
    async def _calculate_base_scores(self, danmaku_list: List[DanmakuItem]) -> List[DanmakuItem]:
        """
        计算每条弹幕的基础评分
        基于配置的权重参数
        """
        current_persona = persona_engine.state
        
        for item in danmaku_list:
            score = 0.0
            
            # 1. 内容相关性评分 (30%)
            content_score = self._calculate_content_relevance(item.message)
            score += content_score * self._weights.get("content_relevance", 0.3)
            
            # 2. 发送者等级评分 (20%)
            sender_score = min(item.sender_level / 10, 1.0)  # 等级1-10映射到0.1-1.0
            score += sender_score * self._weights.get("sender_level", 0.2)
            
            # 3. 情感匹配度评分 (25%)
            emotional_score = self._calculate_emotional_match(
                item.message, 
                current_persona.mood,
                current_persona.darkness
            )
            score += emotional_score * self._weights.get("emotional_match", 0.25)
            
            # 4. 时效性评分 (15%)
            time_diff = (datetime.now() - item.timestamp).total_seconds()
            timeliness_score = max(0, 1 - (time_diff / 300))  # 5分钟内逐渐降低
            score += timeliness_score * self._weights.get("timeliness", 0.15)
            
            # 5. 人格一致性评分 (10%)
            consistency_score = self._calculate_persona_consistency(item.message)
            score += consistency_score * self._weights.get("persona_consistency", 0.1)
            
            item.content_score = content_score
            item.emotional_match_score = emotional_score
            item.priority = score
        
        # 按优先级排序
        danmaku_list.sort(key=lambda x: x.priority, reverse=True)
        
        return danmaku_list
    
    def _calculate_content_relevance(self, message: str) -> float:
        """计算内容相关性"""
        # 基于关键词匹配
        keywords = ["超天酱", "主播", "直播", "唱歌", "游戏", "聊天"]
        message_lower = message.lower()
        
        match_count = sum(1 for kw in keywords if kw in message_lower)
        return min(match_count / 3, 1.0)  # 最多3个关键词匹配得满分
    
    def _calculate_emotional_match(self, message: str, mood: float, darkness: float) -> float:
        """计算情感匹配度"""
        message_lower = message.lower()
        
        # 积极情绪关键词
        positive_words = ["好棒", "喜欢", "可爱", "加油", "支持", "好听", "厉害"]
        # 消极情绪关键词
        negative_words = ["不好", "讨厌", "失望", "难过", "生气", "无聊"]
        # 阴暗/深度话题
        dark_words = ["黑暗", "痛苦", "绝望", "孤独", "死亡", "意义"]
        
        positive_count = sum(1 for w in positive_words if w in message_lower)
        negative_count = sum(1 for w in negative_words if w in message_lower)
        dark_count = sum(1 for w in dark_words if w in message_lower)
        
        # 计算消息的情感倾向
        if positive_count > negative_count:
            message_mood = 0.7  # 积极
        elif negative_count > positive_count:
            message_mood = 0.3  # 消极
        else:
            message_mood = 0.5  # 中性
        
        # 计算与当前心情的匹配度
        mood_diff = abs(message_mood - mood)
        base_match = 1 - mood_diff
        
        # 阴暗度调整
        if darkness > 0.6 and dark_count > 0:
            base_match += 0.2  # 高阴暗度时，深度话题匹配度增加
        
        return min(base_match, 1.0)
    
    def _calculate_persona_consistency(self, message: str) -> float:
        """计算人格一致性"""
        # 检查是否符合主播人设
        persona_keywords = ["超天酱", "小天使", "互联网", "直播"]
        message_lower = message.lower()
        
        match_count = sum(1 for kw in persona_keywords if kw in message_lower)
        return min(match_count / 2, 1.0)
    
    async def _ai_select_danmaku(self, scored_danmaku: List[DanmakuItem]) -> Optional[DanmakuItem]:
        """
        使用AI进行智能选择
        传入评分最高的前5条弹幕供AI选择
        """
        if not scored_danmaku:
            return None
        
        # 负载越高，发送给 AI 的候选越少，降低排队时的推理成本。
        top_candidates = scored_danmaku[:self._load_profile.ai_candidate_limit]
        
        # 构建选择提示
        current_persona = persona_engine.state
        
        prompt = f"""你是一位虚拟主播"{settings.persona.streamer_name}"，正在直播。

当前心情状态：
- 心情值: {current_persona.mood:.2f} (0-1，越高越积极)
- 阴暗度: {current_persona.darkness:.2f} (0-1，越高越阴暗)
- 压力值: {current_persona.stress:.2f} (0-1，越高压力越大)
- 回复激进程度: {persona_engine.behavior.reply_aggressiveness:.2f}

以下是待选择的弹幕候选（已按优先级排序）：

"""
        
        for i, item in enumerate(top_candidates, 1):
            prompt += f"{i}. [{item.nickname}] {item.message}\n"
            prompt += f"   优先级: {item.priority:.3f}, 情感匹配: {item.emotional_match_score:.3f}\n\n"
        
        prompt += """请从以上弹幕中选择一条进行回复。考虑因素：
1. 是否符合当前心情状态
2. 是否有趣或值得回复
3. 是否有助于互动氛围
4. 是否避免重复或无聊的内容

请直接回复以上候选的编号，如果不适合回复任何弹幕，请回复"0"。"""
        
        try:
            lease = concurrency_gate.try_acquire(
                "ai:danmaku_selector",
                settings.rate_limit.ai_selector_concurrency,
            )
            if lease is None:
                logger.warning("弹幕选择 AI 容量已满，使用本地最高分候选")
                return top_candidates[0]
            messages = [
                {"role": "system", "content": f"你是{settings.persona.streamer_name}，一个互联网天使主播。"},
                {"role": "user", "content": prompt}
            ]
            
            try:
                response = await ai_service.run(
                    messages=messages,
                    model=settings.ai.danmaku_selector_model or settings.ai.default_model,
                    temperature=0.3,
                    timeout=settings.ai.danmaku_selector_timeout,
                )
            finally:
                lease.release()
            
            if response and response.get("reply"):
                content = response["reply"].strip()
                
                # 解析AI的选择
                for i in range(1, len(top_candidates) + 1):
                    if str(i) in content[:10]:  # 检查回复开头是否包含编号
                        selected = top_candidates[i-1]
                        logger.info(f"AI选择弹幕 [{i}]: {selected.message[:30]}...")
                        return selected
                
                logger.debug("AI选择不回复任何弹幕")
                return None
            
        except Exception as e:
            logger.error(f"AI选择弹幕失败: {e}")
        
        # AI选择失败时，返回评分最高的一条
        if scored_danmaku:
            logger.debug("AI选择失败，使用评分最高的弹幕")
            return scored_danmaku[0]
        
        return None
    
    def get_selector_stats(self) -> dict:
        """获取选择器统计信息"""
        return {
            "total_selections": self._selection_count,
            "last_selection_time": self._last_selection_time.isoformat() if self._last_selection_time else None,
            "weights": self._weights
        }


# 全局弹幕选择器实例
danmaku_selector = DanmakuSelector()
