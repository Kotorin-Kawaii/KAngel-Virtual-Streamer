from pydantic import BaseModel, Field
from typing import Optional


class PersonaState(BaseModel):
    """人格状态"""
    mood: float = Field(default=0.6, ge=0.0, le=1.0, description="心情值 0~1")
    darkness: float = Field(default=0.2, ge=0.0, le=1.0, description="阴暗度 0~1")
    stress: float = Field(default=0.3, ge=0.0, le=1.0, description="压力值 0~1")


class InternalPersonaState(BaseModel):
    """仅供后端决策使用的细粒度状态，不进入现有前端协议。"""
    arousal: float = Field(default=0.5, ge=0.0, le=1.0, description="兴奋/激活程度")
    fatigue: float = Field(default=0.2, ge=0.0, le=1.0, description="直播疲劳程度")
    attachment: float = Field(default=0.55, ge=0.0, le=1.0, description="对观众整体的依恋")
    confidence: float = Field(default=0.65, ge=0.0, le=1.0, description="当前自信程度")


class InternalStateDelta(BaseModel):
    """内部状态的一次确定性变化。"""
    arousal: float = Field(default=0.0, ge=-1.0, le=1.0)
    fatigue: float = Field(default=0.0, ge=-1.0, le=1.0)
    attachment: float = Field(default=0.0, ge=-1.0, le=1.0)
    confidence: float = Field(default=0.0, ge=-1.0, le=1.0)


class PersonaBehavior(BaseModel):
    """行为倾向"""
    reply_aggressiveness: float = Field(default=0.4, ge=0.0, le=1.0, description="回复激进程度")
    ignore_probability: float = Field(default=0.1, ge=0.0, le=1.0, description="忽略弹幕概率")


class EmotionDelta(BaseModel):
    """情绪变化建议"""
    mood: float = Field(..., ge=-1.0, le=1.0)
    stress: float = Field(..., ge=-1.0, le=1.0)
    darkness: float = Field(..., ge=-1.0, le=1.0)


class PersonaDecision(BaseModel):
    """人格决策结果"""
    danmakuID: str
    reason: Optional[str] = None
    emotion_delta: EmotionDelta


class SentenceWithEmotion(BaseModel):
    """带情绪的句子"""
    emotion: str
    text: str


class AIReply(BaseModel):
    """AI回复"""
    emotions: list[str]
    sentences: list[SentenceWithEmotion]
