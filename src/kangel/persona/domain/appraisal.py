"""P22 事件评价的受限领域模型；不保存模型自由推理。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional

from .state import EmotionDelta


class EventTriggerClass(str, Enum):
    AFFIRMATION = "affirmation"
    COOPERATIVE_RESPONSE = "cooperative_response"
    DISTRESS_SHARE = "distress_share"
    PRESSURE_OR_DEMAND = "pressure_or_demand"
    BOUNDARY_CHALLENGE = "boundary_challenge"
    ACTIVITY_PROGRESS = "activity_progress"
    NEUTRAL_INTERACTION = "neutral_interaction"


@dataclass(frozen=True)
class EventAppraisal:
    """模型只可描述事件维度；状态投影仍由后端确定性逻辑负责。"""

    trigger_class: EventTriggerClass
    reward_or_threat: float
    affiliation: float
    agency_or_pressure: float
    novelty: float
    confidence: float

    @classmethod
    def parse(cls, value: Any) -> Optional["EventAppraisal"]:
        """只接受完整、枚举合法的模型评价；无效内容由调用方安全回退。"""
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                trigger_class=EventTriggerClass(str(value.get("trigger_class", ""))),
                reward_or_threat=cls._signed(value.get("reward_or_threat")),
                affiliation=cls._signed(value.get("affiliation")),
                agency_or_pressure=cls._signed(value.get("agency_or_pressure")),
                novelty=cls._signed(value.get("novelty")),
                confidence=cls._unit(value.get("confidence")),
            )
        except (TypeError, ValueError):
            return None

    @classmethod
    def from_mapping(cls, value: Any, *, fallback: "EventAppraisal") -> "EventAppraisal":
        return cls.parse(value) or fallback

    @staticmethod
    def _signed(value: Any) -> float:
        return max(-1.0, min(1.0, float(value)))

    @staticmethod
    def _unit(value: Any) -> float:
        return max(0.0, min(1.0, float(value)))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["trigger_class"] = self.trigger_class.value
        return data


class EventAppraisalProjector:
    """把有限事件维度确定性投影为 P2 三轴变化。

    这里是模型与人格状态之间唯一的转换层。模型不能直接写入三轴；每个触发
    类别只提供小幅、可测试的基线，再按模型受限评价的置信度衰减并裁剪。
    """

    _TRIGGER_BASELINES: dict[EventTriggerClass, tuple[float, float, float]] = {
        EventTriggerClass.AFFIRMATION: (.012, -.006, -.004),
        EventTriggerClass.COOPERATIVE_RESPONSE: (.016, -.012, -.006),
        EventTriggerClass.DISTRESS_SHARE: (-.006, .012, .002),
        EventTriggerClass.PRESSURE_OR_DEMAND: (-.010, .022, .006),
        EventTriggerClass.BOUNDARY_CHALLENGE: (-.020, .032, .012),
        EventTriggerClass.ACTIVITY_PROGRESS: (.010, -.008, -.003),
        EventTriggerClass.NEUTRAL_INTERACTION: (0.0, 0.0, 0.0),
    }
    _LIMITS = (.055, .065, .045)

    def project(self, appraisal: EventAppraisal) -> EmotionDelta:
        confidence = max(0.0, min(1.0, appraisal.confidence))
        mood, stress, darkness = self._TRIGGER_BASELINES[appraisal.trigger_class]

        # 奖励/威胁是全局主轴，亲和度只产生较小的被连接感；压力维度为负时
        # 表示被施压，正时表示主播仍拥有互动主动性。
        mood += appraisal.reward_or_threat * .032 + appraisal.affiliation * .012
        stress += -appraisal.reward_or_threat * .018 - appraisal.agency_or_pressure * .024
        darkness += -appraisal.reward_or_threat * .010 - appraisal.affiliation * .006
        if appraisal.agency_or_pressure < 0:
            darkness += -appraisal.agency_or_pressure * .008

        # 新颖度仅轻微提高事件权重，不单独制造情绪；避免“新奇”被误认为正向。
        novelty_scale = 1 + max(0.0, appraisal.novelty) * .12
        values = (mood * confidence * novelty_scale,
                  stress * confidence * novelty_scale,
                  darkness * confidence * novelty_scale)
        return EmotionDelta(**{
            axis: max(-limit, min(limit, value))
            for axis, limit, value in zip(("mood", "stress", "darkness"), self._LIMITS, values)
        })


event_appraisal_projector = EventAppraisalProjector()
