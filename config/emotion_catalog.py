"""模型与服务端共享的情绪动作目录。"""

from typing import Dict, Tuple


EMOTION_GROUPS: Dict[str, Tuple[str, ...]] = {
    "positive": ("开心", "喜欢", "得意", "卖萌", "兴奋", "温柔", "亢奋", "大笑"),
    "intimate_performance": ("害羞", "撒娇", "自恋", "做作", "帅气", "打招呼", "笑着挥手"),
    "negative": ("生气", "委屈", "无语", "尴尬", "伤心", "焦虑", "困倦", "疲惫", "厌恶", "害怕"),
    "intense_dark": ("阴暗", "暴怒", "毒舌", "嘲讽", "崩溃", "冷笑", "震惊"),
    "neutral_action": ("眼神飘忽", "祷告", "认真", "思考", "惊讶", "搞怪", "宅系"),
}

AVAILABLE_EMOTIONS: Tuple[str, ...] = tuple(
    emotion
    for group in EMOTION_GROUPS.values()
    for emotion in group
)
