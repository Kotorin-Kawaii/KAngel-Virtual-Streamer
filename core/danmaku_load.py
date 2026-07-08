"""弹幕负载分级与回复节奏策略。"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DanmakuLoadProfile:
    level: str
    current_rate: int
    threshold: int
    min_selection_interval_seconds: float
    force_selection_after_seconds: float
    trigger_probability_multiplier: float
    ai_candidate_limit: int

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_danmaku_load(
    current_rate: int,
    frequency_threshold: int,
) -> DanmakuLoadProfile:
    """将每分钟弹幕数映射为稳定、可测试的回复节奏。"""
    rate = max(0, int(current_rate))
    threshold = max(1, int(frequency_threshold))
    if rate < threshold:
        return DanmakuLoadProfile("normal", rate, threshold, 2.0, 10.0, 1.0, 5)
    if rate < threshold * 2:
        return DanmakuLoadProfile("busy", rate, threshold, 3.0, 9.0, 0.85, 4)
    if rate < threshold * 4:
        return DanmakuLoadProfile("overloaded", rate, threshold, 5.0, 9.0, 0.60, 3)
    return DanmakuLoadProfile("critical", rate, threshold, 8.0, 12.0, 0.35, 2)
