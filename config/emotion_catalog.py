"""模型与服务端共享的情绪动作目录。

目录的粒度以「观众能否看出差别」为准：前端每个标定都要对应一段独立的动画，
不保留和其它标定逐字节相同的条目。历史上的 `亢奋`/`大笑`/`笑着挥手`/`困倦`/`毒舌`
就是这种情况（分别与 `兴奋`/`开心`/`疲惫`/`嘲讽` 共用同一段画面），已经移除；
补上专属素材之后可以再加回来。
"""

from typing import Dict, Tuple


EMOTION_GROUPS: Dict[str, Tuple[str, ...]] = {
    "positive": ("开心", "喜欢", "得意", "卖萌", "兴奋", "温柔"),
    "intimate_performance": ("害羞", "撒娇", "自恋", "做作", "帅气", "打招呼"),
    "negative": ("生气", "委屈", "无语", "尴尬", "伤心", "焦虑", "疲惫", "厌恶", "害怕"),
    "intense_dark": ("阴暗", "暴怒", "嘲讽", "崩溃", "冷笑", "震惊"),
    "neutral_action": ("眼神飘忽", "祷告", "认真", "思考", "惊讶", "搞怪", "宅系"),
}

AVAILABLE_EMOTIONS: Tuple[str, ...] = tuple(
    emotion
    for group in EMOTION_GROUPS.values()
    for emotion in group
)
