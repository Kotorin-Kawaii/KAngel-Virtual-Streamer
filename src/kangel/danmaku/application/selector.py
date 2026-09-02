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
from .attention_metrics import AttentionOutcome, attention_gate_metrics
from kangel.persona.application.engine import PersonaEngine, persona_engine
from kangel.persona.application.prompt_ram import prompt_ram_service
from kangel.stream.application.metadata import stream_metadata_pusher
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


@dataclass(frozen=True)
class AttentionDecision:
    """注意力闸门一次判定的结果。

    只有 ``SELECTED`` 才带候选。其余情形都返回 ``selected is None``，但
    **原因不能被混成一件事**：``IGNORED`` 是主播自己决定这一轮谁都不读，
    四种 ``DEFERRED_*`` 是系统状况（并发满、模型故障、输出不可解析、本地异常）
    导致的让行。让行不 claim、不标记已回复、不删除候选，后续 tick 可以重判。
    """

    outcome: AttentionOutcome
    selected: Optional[DanmakuItem] = None

    @property
    def is_deferral(self) -> bool:
        return self.outcome.is_deferral


def parse_attention_choice(content: str, candidate_count: int) -> Optional[int]:
    """把模型的回复解析成编号；不可解析就返回 ``None``。

    严格到只接受「去掉首尾空白后正好一个完整整数 token」：``0`` 表示明确不读，
    ``1..candidate_count`` 表示选中对应候选。越界、多个数字、带标点、混了
    自然语言——**一律返回 ``None``，绝不猜测成一次选择**。

    之所以不能宽松：旧实现用 ``str(i) in content[:10]`` 做子串匹配，
    「``0，1号也不太合适``」会被读成「选 1 号」，也就是把一次**拒绝**
    解析成了一次选择。宁可这一轮让行（可观测、可重判），也不能编一个选择出来。
    """
    if not isinstance(content, str):
        return None
    tokens = content.split()
    if len(tokens) != 1:
        return None
    token = tokens[0]
    # isdecimal() 排除正负号、小数点、罗马数字与 "²" 这类字符；全角数字
    # （"１"）是无歧义的十进制数字，予以接受。
    if not token.isdecimal():
        return None
    try:
        index = int(token)
    except ValueError:  # pragma: no cover - isdecimal 已保证可转换
        return None
    if index < 0 or index > max(0, candidate_count):
        return None
    return index


class DanmakuSelector:
    """弹幕选择器"""
    
    def __init__(self, *, clock=None, random_value=None, pool: DanmakuPool = danmaku_pool):
        self._lock = asyncio.Lock()
        self._pool = pool
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
        async with self._lock:
            # 调用方传入的是快照；等待锁期间其他连接可能已选中其中的条目。
            available_danmaku = [
                item for item in available_danmaku if item.is_available_for_reply()
            ]
            if not available_danmaku:
                return None

            start_time = datetime.now()

            try:
                # 第一步：计算每条弹幕的基础评分
                scored_danmaku = await self._calculate_base_scores(available_danmaku)

                # 第二步：使用AI进行智能选择
                decision = await self._ai_select_danmaku(scored_danmaku)
                selected = decision.selected

                processing_time = (datetime.now() - start_time).total_seconds() * 1000

                if selected and await self._pool.claim_for_reply(selected.id):
                    self._last_selection_at = self._clock()
                    self._last_selection_time = datetime.now()
                    self._selection_count += 1

                    return SelectionResult(
                        selected_danmaku=selected,
                        selection_reason="AI智能选择",
                        confidence_score=selected.priority,
                        processing_time_ms=processing_time
                    )

                return None

            except Exception as e:
                logger.error(f"弹幕选择过程出错: {e}")
                # 本地异常也是让行，不是主播的决定：不 claim、不标记已回复、
                # 不删除候选。旧实现在这里 claim 了「最高优先级」那条，等于任何
                # 一个本地错误都能把注意力闸门整体绕过——闸门存在的意义就是
                # 「看见 ≠ 读过」，故障绝不能替主播做「一定回复」的决定。
                attention_gate_metrics.record(
                    AttentionOutcome.DEFERRED_LOCAL_ERROR,
                    candidate_count=len(available_danmaku),
                )
                return None
    
    async def _calculate_base_scores(self, danmaku_list: List[DanmakuItem]) -> List[DanmakuItem]:
        """
        计算每条弹幕的基础评分
        基于配置的权重参数
        """
        current_persona = persona_engine.state
        # P30：「正在等谁回话」既要进提示词，也要落到本地打分里——本地分决定
        # 哪些候选进得了 AI 的视野（`ai_candidate_limit` 截断），排在窗口外面的
        # 人就等于没被考虑过。注意力闸门本身不再有绕过 AI 的兜底路径。
        awaiting = self._awaiting_notes()
        bonus = float(settings.prompt_ram.selector_bonus)

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
            # 秒级新鲜度已经远高于业务需要；截断微秒可避免同批/相邻批评分
            # 因执行耗时产生不可观测但会破坏稳定排序的浮点抖动。
            time_diff = int(max(0.0, (datetime.now() - item.timestamp).total_seconds()))
            timeliness_score = max(0, 1 - (time_diff / 300))  # 5分钟内逐渐降低
            score += timeliness_score * self._weights.get("timeliness", 0.15)
            
            # 5. 人格一致性评分 (10%)
            consistency_score = self._calculate_persona_consistency(item.message)
            score += consistency_score * self._weights.get("persona_consistency", 0.1)
            
            item.content_score = content_score
            item.emotional_match_score = emotional_score
            if awaiting and bonus > 0:
                subject_id = self._item_subject_id(item)
                if subject_id and subject_id in awaiting:
                    score = min(1.0, score + bonus)
            item.priority = score
        
        # 按优先级排序
        danmaku_list.sort(key=lambda x: x.priority, reverse=True)
        
        return danmaku_list
    
    @staticmethod
    def _awaiting_notes() -> Dict[str, str]:
        """P30 工作记忆里「正在等谁回话」的 subject_id -> 念头映射。"""
        try:
            return prompt_ram_service.build_for_selector(
                stream_metadata_pusher.get_current_stream_session_id() or ""
            )
        except Exception as exc:
            logger.debug(f"读取工作记忆等待映射失败: {exc}")
            return {}

    @staticmethod
    def _item_subject_id(item: DanmakuItem) -> Optional[str]:
        """按已核验身份匹配候选；昵称字符串永远不参与匹配。"""
        identity = getattr(item, "viewer_identity", None)
        subject_id = getattr(identity, "subject_id", None)
        if isinstance(subject_id, str) and subject_id.strip():
            return subject_id.strip()
        return None

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
    
    async def _ai_select_danmaku(self, scored_danmaku: List[DanmakuItem]) -> AttentionDecision:
        """注意力闸门：由模型决定这一轮读谁、还是谁都不读。

        返回 ``AttentionDecision`` 而不是 ``Optional[DanmakuItem]``，因为
        「没有选中」有五种完全不同的原因，调用方与指标必须分得清：一种是主播
        自己不想读（``IGNORED``），四种是系统让行（``DEFERRED_*``）。

        这里**没有任何兜底选择**。并发满、模型失败、输出不可解析时都让行，
        绝不退化成「选本地最高分那条」——那等于让故障替主播做出「一定回复」的
        决定，`candidate_count == 1` 时更直接变成「单候选自动已读」。
        让行不 claim、不标记已回复、不删除候选，后续 tick 会重新判定。
        """
        if not scored_danmaku:
            # 调用方已保证非空；这里没有做过任何判定，所以不记任何计数。
            return AttentionDecision(AttentionOutcome.DEFERRED_LOCAL_ERROR)
        
        # 负载越高，发送给 AI 的候选越少，降低排队时的推理成本。
        top_candidates = scored_danmaku[:self._load_profile.ai_candidate_limit]
        candidate_count = len(top_candidates)
        
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
        
        awaiting = self._awaiting_notes()
        annotated = False
        for i, item in enumerate(top_candidates, 1):
            prompt += f"{i}. [{item.nickname}] {item.message}\n"
            prompt += f"   优先级: {item.priority:.3f}, 情感匹配: {item.emotional_match_score:.3f}\n"
            note = awaiting.get(self._item_subject_id(item) or "")
            if note:
                prompt += f"   ← 你正在等这个人的回话（{note}）\n"
                annotated = True
            prompt += "\n"

        if annotated:
            prompt += "被标注的候选优先，但如果它明显已经离题就不必勉强。\n\n"
        
        prompt += """请从以上弹幕中选择一条进行回复。考虑因素：
1. 是否符合当前心情状态
2. 是否有趣或值得回复
3. 是否有助于互动氛围
4. 是否避免重复或无聊的内容

"""
        # 输出契约写得死一点：解析端只接受一个完整的整数 token，任何多余的
        # 文字、标点或引号都会让这一轮变成「输出不可解析 → 让行」。这不是压缩
        # 提示词（那被本轮明令禁止），而是让「不读」这个决定能被可靠地读出来。
        prompt += (
            "输出格式（严格遵守）：只输出一个数字，不要任何其他文字、标点、引号或解释。\n"
            f"想回复第 N 条就只输出 N（1 到 {candidate_count}）；"
            "这一轮谁都不想回复就只输出 0。"
        )

        try:
            lease = concurrency_gate.try_acquire(
                "ai:danmaku_selector",
                settings.rate_limit.ai_selector_concurrency,
            )
        except Exception as e:
            logger.error(f"注意力闸门取并发票失败，本轮让行: {e}")
            return self._defer(AttentionOutcome.DEFERRED_LOCAL_ERROR, candidate_count)
        if lease is None:
            # 容量满 = 这一轮根本没问过模型，所以主播没有做任何决定。
            logger.warning("弹幕选择 AI 容量已满，本轮让行（不读取任何候选）")
            return self._defer(AttentionOutcome.DEFERRED_CAPACITY, candidate_count)

        try:
            messages = [
                {"role": "system", "content": f"你是{settings.persona.streamer_name}，一个互联网天使主播。"},
                {"role": "user", "content": prompt}
            ]

            try:
                response = await ai_service.run(
                    messages=messages,
                    role="danmaku_selector",
                    model=settings.ai.danmaku_selector_model or settings.ai.default_model,
                    model_mode="role_hint",
                    temperature=0.3,
                    timeout=settings.ai.danmaku_selector_timeout,
                )
            finally:
                lease.release()
        except Exception as e:
            # AIService 内部已经做过供应商回退；走到这里说明全都失败了。
            # 本 tick 就此结束，不做任何无界同步重试。
            logger.error(f"注意力闸门调用失败，本轮让行: {e}")
            return self._defer(AttentionOutcome.DEFERRED_MODEL_FAILURE, candidate_count)

        if not response or not response.get("reply"):
            logger.warning("注意力闸门无有效返回，本轮让行")
            return self._defer(AttentionOutcome.DEFERRED_MODEL_FAILURE, candidate_count)

        content = str(response["reply"]).strip()
        index = parse_attention_choice(content, candidate_count)
        if index is None:
            # 歧义/越界/混杂输出：不猜。这一路单独计数，才能把「解析失败率」
            # 和「主播主动忽略率」分开看——否则换模型时两者会互相掩盖。
            logger.warning("注意力闸门输出不可解析（长度 %d），本轮让行", len(content))
            logger.debug("不可解析的注意力输出（截断）: %s", content[:20])
            return self._defer(AttentionOutcome.DEFERRED_INVALID_OUTPUT, candidate_count)

        if index == 0:
            logger.info("注意力闸门：这一轮不读任何弹幕（%d 条候选）", candidate_count)
            attention_gate_metrics.record(
                AttentionOutcome.IGNORED, candidate_count=candidate_count
            )
            return AttentionDecision(AttentionOutcome.IGNORED)

        selected = top_candidates[index - 1]
        logger.info(f"AI选择弹幕 [{index}]: {selected.message[:30]}...")
        attention_gate_metrics.record(
            AttentionOutcome.SELECTED, candidate_count=candidate_count
        )
        return AttentionDecision(AttentionOutcome.SELECTED, selected)

    @staticmethod
    def _defer(outcome: AttentionOutcome, candidate_count: int) -> AttentionDecision:
        """让行：记一次计数，什么都不选。绝不 claim、不标记已回复、不删除候选。"""
        attention_gate_metrics.record(outcome, candidate_count=candidate_count)
        return AttentionDecision(outcome)
    
    def get_selector_stats(self) -> dict:
        """获取选择器统计信息"""
        return {
            "total_selections": self._selection_count,
            "last_selection_time": self._last_selection_time.isoformat() if self._last_selection_time else None,
            "weights": self._weights
        }


# 全局弹幕选择器实例
danmaku_selector = DanmakuSelector()
