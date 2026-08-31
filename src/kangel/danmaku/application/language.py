"""弹幕语言检测与低密度回复语言策略；不依赖网络或额外模型。"""

from __future__ import annotations

import hashlib
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from config import settings
from kangel.audience.domain.identity import ViewerIdentity
from kangel.infrastructure.database import DatabaseManager, db_manager
from kangel.shared.logging import logger


Language = Literal["zh", "ja", "en", "other", "unknown"]


@dataclass(frozen=True)
class LanguageDetection:
    language: Language
    confidence: float
    is_mixed: bool
    script_chars: int

    @property
    def is_reliable(self) -> bool:
        return (
            self.language != "unknown"
            and self.script_chars >= settings.stream.language_detection_min_script_chars
            and self.confidence >= settings.stream.language_detection_min_confidence
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["is_reliable"] = self.is_reliable
        return data


class LanguageDetector:
    """按 Unicode 脚本粗分类，日语假名优先于共享的汉字。"""

    def detect(self, text: str) -> LanguageDetection:
        counts = {"zh": 0, "ja": 0, "en": 0, "other": 0}
        for char in text or "":
            code = ord(char)
            if 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF:
                counts["ja"] += 1
            elif 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
                counts["zh"] += 1
            elif (0x0041 <= code <= 0x005A) or (0x0061 <= code <= 0x007A):
                counts["en"] += 1
            elif char.isalpha():
                counts["other"] += 1

        # 日语文本常含汉字；只要有足够假名，将汉字一并纳入日语证据。
        if counts["ja"]:
            counts["ja"] += counts["zh"]
            counts["zh"] = 0
        total = sum(counts.values())
        if total == 0:
            return LanguageDetection("unknown", 0.0, False, 0)
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        language, dominant = ordered[0]
        confidence = dominant / total
        active = sum(1 for value in counts.values() if value)
        return LanguageDetection(
            language, round(confidence, 3), active > 1, total
        )


class ReplyLanguagePolicy:
    """只规定表达语言，绝不改变当前弹幕的语义优先级。"""

    def build_prompt_context(
        self, detection: LanguageDetection, *, english_surprise_joke: bool = False
    ) -> dict[str, Any]:
        if not detection.is_reliable:
            return {
                "language": detection.language,
                "is_reliable": False,
                "instruction": "语言证据不足，按现有自然表达回复；不要猜测或解释语言规则。",
            }
        instructions = {
            "zh": "弹幕主要为中文。优先用自然中文回应，保持主播人格，不要解释语言选择。",
            "ja": "弹幕主要为日语。优先用自然日语回应，保持主播人格，不要变成翻译器或解释语言选择。",
            "en": "弹幕主要为英语。优先使用简单自然的英语，必要时可少量中英混合；保持主播人格，不要解释语言规则。",
            "other": "弹幕主要为其他语言。尽量礼貌回应；理解不足时可用简短中文澄清，但不要伪装成翻译器。",
        }
        context = {
            "language": detection.language,
            "is_reliable": True,
            "instruction": instructions[detection.language],
        }
        if english_surprise_joke and detection.language == "en":
            context["english_surprise_joke"] = True
            context["english_surprise_instruction"] = (
                "这是该观众首次可靠的英文互动，且本场仅允许这一次。"
                "可选地用一句很短的俏皮话表达“没想到我会英语吧”的感觉，"
                "随后立刻自然回答当前问题；不必逐字复述、不解释规则，也不要牺牲直接回答。"
            )
        return context


class EnglishSurpriseJokeService:
    """英文首次互动梗的最小持久化与连接级边界。"""

    def __init__(self, database: DatabaseManager = db_manager):
        self.database = database
        self._guest_claims: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._stats: Counter[str] = Counter()

    def record_detection(self, detection: LanguageDetection) -> None:
        """仅记录固定语言类别与可靠性，不保存弹幕或身份。"""
        with self._lock:
            self._stats[f"language_{detection.language}"] += 1
            self._stats["language_reliable" if detection.is_reliable else "language_unreliable"] += 1

    def should_offer(
        self,
        *,
        detection: LanguageDetection,
        identity: ViewerIdentity | None,
        stream_session_id: str | None,
        event_id: str,
    ) -> bool:
        """若条件满足，原子领取本轮唯一的可选英文互动梗。"""
        if (
            not settings.stream.english_surprise_joke_enabled
            or not detection.is_reliable
            or detection.language != "en"
            or not identity
            or not stream_session_id
            or settings.stream.english_surprise_joke_max_per_stream <= 0
            or not self._passes_probability_gate(identity.subject_id, event_id)
        ):
            with self._lock:
                self._stats["english_surprise_not_offered"] += 1
            return False

        guest_key = (identity.session_scope_id, stream_session_id)
        with self._lock:
            if not identity.is_authenticated and guest_key in self._guest_claims:
                self._stats["english_surprise_not_offered"] += 1
                return False
            try:
                claimed = self.database.claim_english_surprise_joke(
                    stream_session_id=stream_session_id,
                    viewer_scope=identity.subject_id,
                    account_id=identity.account_id if identity.is_authenticated else None,
                    used_at=datetime.now(timezone.utc).isoformat(),
                    max_per_stream=settings.stream.english_surprise_joke_max_per_stream,
                )
            except Exception as exc:
                logger.warning("英文首次互动梗状态领取失败，按普通英文回复: %s", exc)
                self._stats["english_surprise_storage_error"] += 1
                return False
            if claimed and not identity.is_authenticated:
                self._guest_claims.add(guest_key)
            self._stats["english_surprise_offered" if claimed else "english_surprise_not_offered"] += 1
            return claimed

    def forget_guest(self, identity: ViewerIdentity | None) -> None:
        """游客断开即清除连接级记录；登录账号仍由数据库控制。"""
        if not identity or identity.is_authenticated:
            return
        with self._lock:
            self._guest_claims = {
                item for item in self._guest_claims
                if item[0] != identity.session_scope_id
            }

    def get_stats(self) -> dict[str, int]:
        """返回固定低基数聚合计数，供受控诊断读取。"""
        with self._lock:
            return dict(sorted(self._stats.items()))

    @staticmethod
    def _passes_probability_gate(subject_id: str, event_id: str) -> bool:
        probability = settings.stream.english_surprise_joke_probability
        if probability <= 0:
            return False
        if probability >= 1:
            return True
        digest = hashlib.sha256(
            f"kangel-english-surprise:{subject_id}:{event_id}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big") / 2**64 < probability


language_detector = LanguageDetector()
reply_language_policy = ReplyLanguagePolicy()
english_surprise_joke_service = EnglishSurpriseJokeService()
