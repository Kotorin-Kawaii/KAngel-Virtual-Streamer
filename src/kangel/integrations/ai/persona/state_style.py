"""把运行时数值状态确定性投影为有限风格旋钮。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _value(state: Mapping[str, float] | object | None, name: str, default: float) -> float:
    if state is None:
        return default
    if isinstance(state, Mapping):
        raw = state.get(name, default)
    else:
        raw = getattr(state, name, default)
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 2)


@dataclass(frozen=True, slots=True)
class PersonaStyleVector:
    warmth: float
    sharpness: float
    brevity: float
    performance_energy: float
    fragility_leak: float
    audience_clinginess: float
    defensive_bravado: float

    def to_prompt(self) -> str:
        return (
            "- 温暖度 " f"{self.warmth:.2f}；尖锐度 {self.sharpness:.2f}；"
            f"简短度 {self.brevity:.2f}；营业能量 {self.performance_energy:.2f}；\n"
            "- 脆弱泄漏 " f"{self.fragility_leak:.2f}；观众黏着 {self.audience_clinginess:.2f}；"
            f"防御性自夸 {self.defensive_bravado:.2f}。\n"
            "观众黏着只指对整体直播间的依恋，不能创造陌生用户亲密关系。"
            "这些值只调整表达强弱：不强制辱骂、死亡/自伤话题、恋爱宣称或身份切换。"
        )


def build_style_vector(
    persona_state: Mapping[str, float] | object | None,
    internal_state: Mapping[str, float] | object | None,
) -> PersonaStyleVector:
    mood = _value(persona_state, "mood", 0.6)
    stress = _value(persona_state, "stress", 0.3)
    darkness = _value(persona_state, "darkness", 0.2)
    arousal = _value(internal_state, "arousal", 0.5)
    fatigue = _value(internal_state, "fatigue", 0.2)
    attachment = _value(internal_state, "attachment", 0.55)
    confidence = _value(internal_state, "confidence", 0.65)

    # 所有旋钮有意保持有限；极端状态也不生成强制行为。
    return PersonaStyleVector(
        warmth=_clamp(0.42 + mood * 0.34 + attachment * 0.12 - stress * 0.12),
        sharpness=_clamp(0.12 + darkness * 0.42 + stress * 0.26 - mood * 0.08),
        brevity=_clamp(0.42 + fatigue * 0.34 + stress * 0.12 - arousal * 0.08),
        performance_energy=_clamp(0.28 + mood * 0.25 + arousal * 0.32 - fatigue * 0.28),
        fragility_leak=_clamp(
            0.08 + stress * 0.28 + fatigue * 0.22 + (1.0 - confidence) * 0.25
        ),
        audience_clinginess=_clamp(0.18 + attachment * 0.52 + (1.0 - mood) * 0.08),
        defensive_bravado=_clamp(
            0.38 + confidence * 0.24 + stress * 0.18 + (1.0 - confidence) * 0.12
        ),
    )


__all__ = ["PersonaStyleVector", "build_style_vector"]
