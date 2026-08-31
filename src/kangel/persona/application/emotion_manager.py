"""
情绪多元化管理模块
提供丰富的情绪选择和随机强度控制
"""

import random
from copy import deepcopy
from collections import Counter
from threading import RLock
from typing import List, Dict, Set, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum

from config.emotion_catalog import AVAILABLE_EMOTIONS
from config.settings import settings
from kangel.shared.logging import logger


class EmotionCategory(Enum):
    """情绪分类"""
    POSITIVE = "positive"  # 积极情绪
    INTIMATE_PERFORMANCE = "intimate_performance"  # 亲密/表现
    NEGATIVE = "negative"  # 消极情绪
    INTENSE_DARK = "intense_dark"  # 强烈/阴暗
    NEUTRAL_ACTION = "neutral_action"  # 中性/动作


@dataclass
class EmotionItem:
    """情绪项"""
    name: str  # 情绪名称
    category: EmotionCategory  # 情绪分类
    weight: float = 1.0  # 基础权重
    mood_bonus: float = 0.0  # 心情加成系数
    darkness_bonus: float = 0.0  # 阴暗加成系数
    stress_bonus: float = 0.0  # 压力加成系数
    description: str = ""  # 描述


class EmotionManager:
    """情绪管理器"""

    # 类别语气契合度低于这个值就不进推荐：多样化不能把语气拧反。
    _CATEGORY_AFFINITY_FLOOR = 0.22

    def __init__(
        self,
        diversity_enabled: Optional[bool] = None,
        cold_bonus: Optional[float] = None,
    ):
        # 情绪数据库
        self._emotions: Dict[str, EmotionItem] = {}

        # 情绪历史记录
        self._recent_emotions: List[str] = []
        self._max_recent_history = 24
        self._lock = RLock()

        # 随机强度 0-1，越高越随机
        self._randomness = 0.3

        # 覆盖策略：默认开启，可用 PERSONA__EMOTION_DIVERSITY_ENABLED=false 回到旧打分。
        persona = getattr(settings, "persona", None)
        self._diversity_enabled = bool(
            getattr(persona, "emotion_diversity_enabled", True)
            if diversity_enabled is None else diversity_enabled
        )
        configured_bonus = float(
            getattr(persona, "emotion_cold_bonus", 1.5)
            if cold_bonus is None else cold_bonus
        )
        self._cold_bonus = configured_bonus if self._diversity_enabled else 0.0
        # 收紧重复判定：近 10 次里出现 2 次就换同类别的另一个动作。
        self._overuse_threshold = 2 if self._diversity_enabled else 3

        # 初始化情绪库
        self._init_emotion_database()

        logger.info("情绪管理器初始化成功")
    
    def _init_emotion_database(self):
        """初始化情绪数据库"""
        emotions = [
            EmotionItem("开心", EmotionCategory.POSITIVE, mood_bonus=2.0, description="开心、快乐的情绪"),
            EmotionItem("喜欢", EmotionCategory.POSITIVE, mood_bonus=2.0, description="喜欢、爱慕的情绪"),
            EmotionItem("得意", EmotionCategory.POSITIVE, mood_bonus=1.2, description="得意、自豪的情绪"),
            EmotionItem("卖萌", EmotionCategory.POSITIVE, mood_bonus=1.5, description="可爱的卖萌表情"),
            EmotionItem("兴奋", EmotionCategory.POSITIVE, mood_bonus=2.0, description="兴奋、激动的情绪"),
            EmotionItem("温柔", EmotionCategory.POSITIVE, mood_bonus=1.5, description="温柔、体贴的情绪"),

            EmotionItem("害羞", EmotionCategory.INTIMATE_PERFORMANCE, mood_bonus=1.0, description="害羞、腼腆的表现"),
            EmotionItem("撒娇", EmotionCategory.INTIMATE_PERFORMANCE, mood_bonus=1.4, description="亲昵撒娇的表现"),
            EmotionItem("自恋", EmotionCategory.INTIMATE_PERFORMANCE, mood_bonus=0.8, darkness_bonus=0.3, description="自恋、自我欣赏的表现"),
            EmotionItem("做作", EmotionCategory.INTIMATE_PERFORMANCE, mood_bonus=0.4, darkness_bonus=0.5, description="刻意夸张的表演"),
            EmotionItem("帅气", EmotionCategory.INTIMATE_PERFORMANCE, mood_bonus=1.0, description="自信帅气的表现"),
            EmotionItem("打招呼", EmotionCategory.INTIMATE_PERFORMANCE, mood_bonus=0.8, description="主动打招呼的动作"),

            EmotionItem("生气", EmotionCategory.NEGATIVE, stress_bonus=1.8, description="生气、愤怒的情绪"),
            EmotionItem("委屈", EmotionCategory.NEGATIVE, stress_bonus=1.2, description="委屈、难过的情绪"),
            EmotionItem("无语", EmotionCategory.NEGATIVE, stress_bonus=1.0, description="无语、无奈的情绪"),
            EmotionItem("尴尬", EmotionCategory.NEGATIVE, stress_bonus=1.0, description="尴尬、难为情的情绪"),
            EmotionItem("伤心", EmotionCategory.NEGATIVE, stress_bonus=1.5, description="伤心、难过的情绪"),
            EmotionItem("焦虑", EmotionCategory.NEGATIVE, stress_bonus=1.5, description="焦虑、担忧的情绪"),
            EmotionItem("疲惫", EmotionCategory.NEGATIVE, stress_bonus=1.2, description="疲惫、劳累的状态"),
            EmotionItem("厌恶", EmotionCategory.NEGATIVE, stress_bonus=1.6, darkness_bonus=0.5, description="厌恶、排斥的情绪"),
            EmotionItem("害怕", EmotionCategory.NEGATIVE, stress_bonus=1.8, description="害怕、恐惧的情绪"),

            EmotionItem("阴暗", EmotionCategory.INTENSE_DARK, darkness_bonus=2.0, description="阴暗、消极的情绪"),
            EmotionItem("暴怒", EmotionCategory.INTENSE_DARK, darkness_bonus=1.5, stress_bonus=1.5, description="暴怒、狂怒的情绪"),
            EmotionItem("嘲讽", EmotionCategory.INTENSE_DARK, darkness_bonus=1.5, description="嘲讽、挖苦、毒舌的表现"),
            EmotionItem("崩溃", EmotionCategory.INTENSE_DARK, darkness_bonus=1.4, stress_bonus=1.8, description="情绪崩溃的强烈表现"),
            EmotionItem("冷笑", EmotionCategory.INTENSE_DARK, darkness_bonus=1.5, description="冷笑、讥讽的表现"),
            EmotionItem("震惊", EmotionCategory.INTENSE_DARK, stress_bonus=1.2, description="强烈震惊的情绪"),

            EmotionItem("眼神飘忽", EmotionCategory.NEUTRAL_ACTION, description="眼神飘忽不定的动作"),
            EmotionItem("祷告", EmotionCategory.NEUTRAL_ACTION, darkness_bonus=0.3, description="祷告、祈祷的动作"),
            EmotionItem("认真", EmotionCategory.NEUTRAL_ACTION, description="认真、专注的状态"),
            EmotionItem("思考", EmotionCategory.NEUTRAL_ACTION, description="思考、斟酌的状态"),
            EmotionItem("惊讶", EmotionCategory.NEUTRAL_ACTION, stress_bonus=0.4, description="惊讶、意外的情绪"),
            EmotionItem("搞怪", EmotionCategory.NEUTRAL_ACTION, mood_bonus=1.3, description="搞怪、滑稽的动作"),
            EmotionItem("宅系", EmotionCategory.NEUTRAL_ACTION, description="宅系风格的表现"),
        ]

        if tuple(emotion.name for emotion in emotions) != AVAILABLE_EMOTIONS:
            raise RuntimeError("情绪管理器定义与共享情绪目录不一致")
        for emotion in emotions:
            self._emotions[emotion.name] = emotion
    
    def set_randomness(self, randomness: float):
        """设置随机强度 0-1"""
        self._randomness = max(0.0, min(1.0, randomness))
        logger.debug(f"情绪随机强度设置为: {self._randomness:.2f}")
    
    def get_randomness(self) -> float:
        """获取当前随机强度"""
        return self._randomness
    
    def get_available_emotions(self) -> List[str]:
        """获取所有可用的情绪名称"""
        return list(self._emotions.keys())
    
    def get_emotions_by_category(self, category: EmotionCategory) -> List[str]:
        """按分类获取情绪"""
        return [e.name for e in self._emotions.values() if e.category == category]
    
    def select_emotions(
        self, 
        mood: float, 
        stress: float, 
        darkness: float, 
        count: int = 2,
        exclude_recent: bool = True,
        record_selection: bool = True
    ) -> List[str]:
        """
        根据人格状态选择情绪
        
        Args:
            mood: 心情值 0-1
            stress: 压力值 0-1
            darkness: 阴暗度 0-1
            count: 要选择的情绪数量
            exclude_recent: 是否排除最近使用的情绪
            
        Returns:
            选择的情绪列表
        """
        # 计算每个情绪的权重
        weights = {}
        for emotion_name, emotion in self._emotions.items():
            # 基础权重
            weight = emotion.weight
            
            # 根据人格状态调整权重
            weight += emotion.mood_bonus * mood
            weight += emotion.stress_bonus * stress
            weight += emotion.darkness_bonus * darkness
            
            # 排除最近使用的情绪，避免重复
            if exclude_recent and emotion_name in self._recent_emotions:
                weight *= 0.3  # 最近使用过的权重降低
            
            # 根据随机强度调整
            if self._randomness > 0:
                # 添加随机因子
                random_factor = 1.0 + (random.random() - 0.5) * 2 * self._randomness
                weight *= random_factor
            
            # 确保权重为正
            weight = max(0.1, weight)
            weights[emotion_name] = weight
        
        # 使用权重随机选择
        selected_emotions = self._weighted_random_selection(weights, count)
        
        if record_selection:
            self.record_emotions(selected_emotions)
        
        logger.debug(f"选择的情绪: {selected_emotions} (心情: {mood:.2f}, 压力: {stress:.2f}, 阴暗: {darkness:.2f})")
        
        return selected_emotions

    def record_emotions(self, emotions: List[str]) -> None:
        """记录最终实际发送给前端的情绪，而不是模型候选。"""
        with self._lock:
            for emotion in emotions:
                if emotion not in self._emotions:
                    continue
                self._recent_emotions.append(emotion)
            if len(self._recent_emotions) > self._max_recent_history:
                self._recent_emotions = self._recent_emotions[-self._max_recent_history:]

    def restore_history(self, emotions: List[str]) -> None:
        """从数据库恢复最近实际使用历史。"""
        with self._lock:
            self._recent_emotions = []
        self.record_emotions(emotions)

    def get_recent_emotions(self, limit: int = 10) -> List[str]:
        with self._lock:
            return self._recent_emotions[-max(0, limit):].copy()

    def recommend_emotions(
        self,
        mood: float,
        stress: float,
        darkness: float,
        count: int = 5,
        category: Optional[EmotionCategory] = None,
        excluded: Optional[Set[str]] = None,
    ) -> List[str]:
        """按状态、近期频率与冷门程度确定性排序，不提前写入使用历史。"""
        excluded = excluded or set()
        recent = self.get_recent_emotions(10)
        frequencies = Counter(recent)
        staleness = self._staleness_map()
        last = recent[-1] if recent else None
        base = {
            name: self._state_score(item, mood, stress, darkness)
            for name, item in self._emotions.items()
        }
        peak = self._category_peaks(base)
        scored: List[Tuple[float, str, EmotionCategory]] = []
        for name, item in self._emotions.items():
            if name in excluded or (category and item.category != category):
                continue
            if self._diversity_enabled:
                # 类别内归一化后再叠加冷门加成：否则「零加成的中性动作」永远比
                # 「带 2.0 心情加成的正向情绪」低一截，无论多久没用都排不上来。
                fit = base[name] / (peak[item.category] or 1.0)
                score = fit + self._cold_bonus * staleness.get(name, 1.0)
            else:
                score = base[name]
            score /= 1.0 + frequencies[name] * 2.5
            if name == last:
                score *= 0.12
            scored.append((score, name, item.category))
        scored.sort(key=lambda row: (-row[0], row[1]))
        limit = max(0, count)
        # 指定类别时（替换同类动作）保持纯排序，不再二次分配名额。
        if category or not self._diversity_enabled:
            return [name for _, name, _ in scored[:limit]]
        return self._spread_across_categories(scored, mood, stress, darkness, limit)

    @staticmethod
    def _state_score(
        item: EmotionItem, mood: float, stress: float, darkness: float
    ) -> float:
        return (
            item.weight
            + item.mood_bonus * mood
            + item.stress_bonus * stress
            + item.darkness_bonus * darkness
        )

    def _category_peaks(self, base: Dict[str, float]) -> Dict[EmotionCategory, float]:
        peaks: Dict[EmotionCategory, float] = {}
        for name, item in self._emotions.items():
            peaks[item.category] = max(peaks.get(item.category, 0.0), base[name])
        return peaks

    def _staleness_map(self) -> Dict[str, float]:
        """按最后一次出现的位置给出 0-1 的冷门程度；未出现过的取 1.0。"""
        window = self.get_recent_emotions(self._max_recent_history)
        if not window:
            return {}
        span = float(len(window))
        last_seen = {name: index for index, name in enumerate(window)}
        return {
            name: (span - 1 - index) / span
            for name, index in last_seen.items()
        }

    def _category_affinity(
        self, category: EmotionCategory, mood: float, stress: float, darkness: float
    ) -> float:
        """当前状态下这一类表达是否说得过去；决定它能分到几个推荐位。"""
        if category is EmotionCategory.POSITIVE:
            return mood
        if category is EmotionCategory.INTIMATE_PERFORMANCE:
            return 0.25 + 0.55 * mood
        if category is EmotionCategory.NEGATIVE:
            # 心情好且不紧绷时归零，避免把负向动作推给欢快的语境。
            return max(0.0, 0.7 * stress + 0.6 * (0.5 - mood))
        if category is EmotionCategory.INTENSE_DARK:
            return max(0.0, 0.65 * darkness + 0.5 * stress - 0.35 * mood)
        # 中性动作（认真/思考/眼神飘忽等）与任何语气都兼容，恒定可用。
        return 0.45

    def _spread_across_categories(
        self,
        scored: List[Tuple[float, str, EmotionCategory]],
        mood: float,
        stress: float,
        darkness: float,
        limit: int,
    ) -> List[str]:
        """按类别语气契合度分配名额，每类内部再取前几名。

        纯线性打分会让状态决定的同一簇永远占满推荐位（实测六种状态合计只能推出
        18/34 个标定，全部中性动作永远垫底）。这里改成先给说得通的类别分名额，
        再在类别内部取高分项：既保住语气正确，又让模型每轮都看到多种表达。
        """
        buckets: Dict[EmotionCategory, List[Tuple[float, str]]] = {}
        for score, name, category in scored:
            buckets.setdefault(category, []).append((score, name))
        if not buckets or limit <= 0:
            return []
        affinity = {
            category: self._category_affinity(category, mood, stress, darkness)
            for category in buckets
        }
        plausible = [
            category for category in buckets
            if affinity[category] >= self._CATEGORY_AFFINITY_FLOOR
        ] or list(buckets)
        order = sorted(
            plausible,
            key=lambda category: (-affinity[category], category.value),
        )
        total_affinity = sum(affinity[category] for category in order) or 1.0
        quota = {
            category: max(1, round(limit * affinity[category] / total_affinity))
            for category in order
        }
        picked: List[Tuple[float, str]] = []
        for category in order:
            picked.extend(buckets[category][:quota[category]])
        if len(picked) > limit:
            # 名额取整可能超发：整体按分数截断，保留分数最高的 limit 个。
            picked.sort(key=lambda row: (-row[0], row[1]))
            picked = picked[:limit]
        elif len(picked) < limit:
            # 名额不足时按契合度顺序补足，绝不越过语气过滤。
            taken = {name for _, name in picked}
            for category in order:
                for row in buckets[category]:
                    if len(picked) >= limit:
                        break
                    if row[1] not in taken:
                        picked.append(row)
                        taken.add(row[1])
                if len(picked) >= limit:
                    break
        # 名额决定「有哪些」，分数决定「谁排在前面」。模型对列表头部有明显的锚定
        # 倾向，所以冷门项必须能靠分数抢到头部，否则配额只是把它们排到看不见的尾部。
        picked.sort(key=lambda row: (-row[0], row[1]))
        return [name for _, name in picked]

    def _cold_emotions(
        self, mood: float, stress: float, darkness: float, count: int = 8
    ) -> List[str]:
        """近期完全没出现、且语气说得过去的标定，用来给模型正向拉动。"""
        window = set(self.get_recent_emotions(self._max_recent_history))
        cold = {name for name in self._emotions if name not in window}
        if not cold:
            return []
        return self.recommend_emotions(
            mood, stress, darkness, count=count,
            excluded=set(self._emotions) - cold,
        )

    def build_prompt_context(self, mood: float, stress: float, darkness: float) -> Dict:
        """构建供回复模型阅读的近期情绪使用上下文。"""
        recent = self.get_recent_emotions(10)
        frequencies = Counter(recent)
        overused = [
            name for name, frequency in frequencies.most_common()
            if frequency >= self._overuse_threshold + 1
            or (recent and recent[-1] == name and frequency >= self._overuse_threshold)
        ]
        return {
            "recent_emotions": recent,
            "frequency": dict(frequencies),
            "overused_emotions": overused,
            "recommended_emotions": self.recommend_emotions(
                mood, stress, darkness, count=8
            ),
            "unused_emotions": self._cold_emotions(mood, stress, darkness, count=8),
            "available_emotions": self.get_available_emotions(),
        }

    def diversify_reply(
        self,
        reply_data: Dict,
        mood: float,
        stress: float,
        darkness: float,
    ) -> Dict:
        """修正非法或机械重复的动作标签，并保持 emotions/sentences 一一对应。"""
        result = deepcopy(reply_data) if isinstance(reply_data, dict) else {}
        sentences = [item for item in result.get("sentences", []) if isinstance(item, dict)]
        model_emotions = result.get("emotions", []) if isinstance(result.get("emotions"), list) else []
        if not sentences:
            valid = [name for name in model_emotions if name in self._emotions]
            result["emotions"] = valid
            return result

        recent = self.get_recent_emotions(10)
        recent_counts = Counter(recent)
        selected: List[str] = []
        for index, sentence in enumerate(sentences):
            original = sentence.get("emotion") or (
                model_emotions[index] if index < len(model_emotions) else ""
            )
            candidate = str(original)
            should_replace = (
                candidate not in self._emotions
                or candidate in selected
                or self._is_overused(candidate, recent, recent_counts)
            )
            if should_replace and not self._is_state_critical(candidate, mood, stress, darkness):
                candidate = self._choose_replacement(
                    candidate, set(selected), mood, stress, darkness
                )
            if candidate not in self._emotions:
                fallback = self.recommend_emotions(
                    mood, stress, darkness, count=1, excluded=set(selected)
                )
                candidate = fallback[0] if fallback else "眼神飘忽"
            sentence["emotion"] = candidate
            selected.append(candidate)

        result["sentences"] = sentences
        result["emotions"] = selected
        return result

    def _is_overused(self, emotion: str, recent: List[str], frequencies: Counter) -> bool:
        if not emotion or emotion not in self._emotions:
            return True
        return (
            (bool(recent) and recent[-1] == emotion)
            or frequencies[emotion] >= self._overuse_threshold
        )

    def _is_state_critical(self, emotion: str, mood: float, stress: float, darkness: float) -> bool:
        return (
            (emotion == "暴怒" and stress >= 0.88)
            or (emotion == "阴暗" and darkness >= 0.82)
            or (emotion == "委屈" and mood <= 0.18 and stress >= 0.6)
        )

    def _choose_replacement(
        self,
        original: str,
        selected: Set[str],
        mood: float,
        stress: float,
        darkness: float,
    ) -> str:
        item = self._emotions.get(original)
        same_category = self.recommend_emotions(
            mood,
            stress,
            darkness,
            count=len(self._emotions),
            category=item.category if item else None,
            excluded=selected | ({original} if original else set()),
        )
        if same_category:
            return same_category[0]
        if item:
            return original
        fallback = self.recommend_emotions(
            mood, stress, darkness, count=1, excluded=selected
        )
        return fallback[0] if fallback else original
    
    def _weighted_random_selection(self, weights: Dict[str, float], count: int) -> List[str]:
        """
        加权随机选择算法
        
        Args:
            weights: 情绪名称到权重的映射
            count: 要选择的数量
            
        Returns:
            选择的情绪列表，不会有重复
        """
        selected = []
        available_weights = weights.copy()
        
        for _ in range(count):
            if not available_weights:
                break
            
            # 计算总权重
            total_weight = sum(available_weights.values())
            if total_weight <= 0:
                break
            
            # 随机选择
            random_value = random.uniform(0, total_weight)
            current_sum = 0
            
            for emotion_name, weight in available_weights.items():
                current_sum += weight
                if current_sum >= random_value:
                    selected.append(emotion_name)
                    # 移除已选择的，避免重复
                    del available_weights[emotion_name]
                    break
        
        return selected
    
    def get_emotion_info(self, emotion_name: str) -> Optional[Dict]:
        """获取情绪详细信息"""
        emotion = self._emotions.get(emotion_name)
        if not emotion:
            return None
        
        return {
            "name": emotion.name,
            "category": emotion.category.value,
            "weight": emotion.weight,
            "mood_bonus": emotion.mood_bonus,
            "darkness_bonus": emotion.darkness_bonus,
            "stress_bonus": emotion.stress_bonus,
            "description": emotion.description
        }
    
    def get_statistics(self) -> Dict:
        """获取情绪管理器统计信息"""
        category_counts = {}
        for emotion in self._emotions.values():
            cat = emotion.category.value
            category_counts[cat] = category_counts.get(cat, 0) + 1
        
        recent = self.get_recent_emotions(self._max_recent_history)
        return {
            "total_emotions": len(self._emotions),
            "category_counts": category_counts,
            "recent_emotions": recent,
            "recent_frequency": dict(Counter(recent)),
            "recent_distinct": len(set(recent)),
            "recent_coverage": round(len(set(recent)) / max(1, len(self._emotions)), 4),
            "diversity_enabled": self._diversity_enabled,
            "cold_bonus": self._cold_bonus,
            "randomness": self._randomness,
            "available_emotions": list(self._emotions.keys())
        }
    
    def reset_recent_history(self):
        """重置最近使用历史"""
        with self._lock:
            self._recent_emotions = []
        logger.debug("情绪使用历史已重置")


# 全局情绪管理器实例
emotion_manager = EmotionManager()
