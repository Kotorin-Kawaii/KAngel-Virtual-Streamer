"""P30 prompt RAM 领域模型：主播「还没闭合的念头」。

只有 dataclass、enum 与纯函数，不做任何 IO，不读配置——寿命由服务层算好之后
以 ``expires_at_monotonic`` 传进来。判活一律用 ``time.monotonic()``，
这样系统时间被改动也不会让条目提前消失或永久滞留。

安全要点：``note`` 是模型基于观众文本生成、随后又会被喂回模型的内容，
等于给观众开了一条延迟生效的注入通道。``sanitize_note`` 是这条链路的第一道防线。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional


class RamKind(str, Enum):
    """念头的四种形态；TTL 由服务层按 kind 分别解析。"""

    AWAITING_VIEWER = "awaiting_viewer"  # 等某位观众回话
    OWED_FOLLOWUP = "owed_followup"  # 自己答应了要做/要说的事
    STANDING_IDEA = "standing_idea"  # 冒出来还想聊的念头，无对象
    HOLDING_BACK = "holding_back"  # 决定暂时不提的事（自我抑制）


class RamState(str, Enum):
    OPEN = "open"  # 活跃，正在注入
    FULFILLED = "fulfilled"  # 被等的人开口了；宽限期内仍注入
    SUPERSEDED = "superseded"  # 同一对象出现更新的条目，被覆盖


# 昵称只用于渲染，永远不参与身份匹配，所以长度可以卡得更死。
NICKNAME_MAX_CHARS = 24

# 删掉能伪造提示词层结构的字符：中文方括号是本仓库层标题的定界符，
# 花括号与反引号能伪造 JSON / 代码块，控制字符能塞入换行。
_FORBIDDEN_NOTE_CHARS = re.compile(r"[【】{}`\x00-\x1f\x7f]")


def sanitize_note(raw: object, *, max_chars: int) -> Optional[str]:
    """把模型给出的念头压成一行安全短文本；不合格返回 ``None``。"""
    if not isinstance(raw, str):
        return None

    # 折叠所有空白，顺带干掉换行与制表符。
    collapsed = " ".join(raw.split())
    if not collapsed:
        return None

    cleaned = _FORBIDDEN_NOTE_CHARS.sub("", collapsed).strip()
    if not cleaned:
        return None

    limit = max(1, int(max_chars))
    return cleaned[:limit]


def sanitize_nickname(raw: object) -> str:
    """昵称同样消毒；它会进渲染文本，但不参与任何身份判定。"""
    return sanitize_note(raw, max_chars=NICKNAME_MAX_CHARS) or ""


@dataclass(frozen=True)
class RamEntry:
    """一条未闭合的念头。不可变；状态变化一律产生新对象。"""

    entry_id: str
    kind: RamKind
    state: RamState
    note: str
    # ViewerIdentity.subject_id，唯一身份键；无对象的念头为 None。
    target_subject_id: Optional[str]
    # 仅供渲染，永不用于匹配。
    target_nickname: str
    stream_session_id: str
    source_danmaku_id: str
    created_at: str  # UTC ISO，仅供后台展示
    expires_at_monotonic: float
    version: int = 1

    def is_active(self, now: float) -> bool:
        if self.state is RamState.SUPERSEDED:
            return False
        return now < self.expires_at_monotonic

    def remaining_seconds(self, now: float) -> float:
        return max(0.0, self.expires_at_monotonic - now)

    def with_state(
        self,
        state: RamState,
        *,
        expires_at_monotonic: float,
    ) -> "RamEntry":
        return RamEntry(
            entry_id=self.entry_id,
            kind=self.kind,
            state=state,
            note=self.note,
            target_subject_id=self.target_subject_id,
            target_nickname=self.target_nickname,
            stream_session_id=self.stream_session_id,
            source_danmaku_id=self.source_danmaku_id,
            created_at=self.created_at,
            expires_at_monotonic=expires_at_monotonic,
            version=self.version + 1,
        )

    def to_admin_dict(self, now: float) -> dict:
        """后台快照。``target_subject_id`` 只允许出现在 ADMIN_ONLY 响应里。"""
        data = asdict(self)
        data["kind"] = self.kind.value
        data["state"] = self.state.value
        data.pop("expires_at_monotonic", None)
        data["remaining_seconds"] = round(self.remaining_seconds(now), 1)
        return data


@dataclass(frozen=True)
class ParsedThought:
    """已消毒、已定 kind，但还没绑定身份与寿命的中间结果。"""

    kind: RamKind
    note: str
    # 模型给出的昵称文本，不可信；只用于和本轮弹幕作者昵称比对。
    claimed_target: str
