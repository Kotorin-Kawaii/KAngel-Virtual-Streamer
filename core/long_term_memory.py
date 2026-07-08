"""登录观众的长期对话链、话题连续性、检索、摘要与遗忘。"""

from __future__ import annotations

import math
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from config import settings
from core.chinese_text_analyzer import chinese_text_analyzer
from core.database_manager import db_manager
from core.memory_policy import account_memory_policy
from models.viewer import ViewerIdentity
from utils.logger import logger


class ConversationTransition(str, Enum):
    NEW = "new"
    CONTINUATION = "continuation"
    CONTRAST = "contrast"
    SUPPLEMENT = "supplement"
    SWITCH = "switch"


class ConversationContinuityAnalyzer:
    """用可解释的中文连接词和话题重合判断本轮如何承接上一轮。"""

    _contrast = re.compile(r"^(?:但是|不过|可是|然而|但|话虽如此)")
    _continuation = re.compile(r"^(?:然后呢?|后来呢?|接着呢?|所以呢?|结果呢?|那怎么办|为什么呢?)")
    _supplement = re.compile(r"^(?:而且|还有|另外|其实|再说|他也|她也|它也|我也|也就是说)")
    _explicit_switch = re.compile(r"^(?:换个话题|不说这个了|说点别的|说起来|对了)[，,：:\s]*")
    _reference = re.compile(
        r"(?:^|[，,。.!！？?\s])(他|她|它|他们|她们|它们|这个|那个|这件事|那件事|这事|那事|这样|那样)"
    )
    _elliptical = re.compile(r"^(?:但是|不过|可是|然后|后来|接着|所以|而且|还有|也|那|这).{0,24}$")
    _dependent_reply = re.compile(
        r"^(?:"
        r"两个都要|三个都要|全都要|全部都要|都要|都喜欢|都可以|都行|都好|"
        r"前者|后者|第[一二两三]|上一个|下一个|"
        r"是的?|对(?:啊|呀|哦|的)?|没错|不是|不要|可以|不可以|为什么"
        r")[！!？?。.…~～]*$"
    )
    _action_confirmation = re.compile(
        r"^.{0,16}(?:放好了|弄好了|准备好了|完成了|做好了|到位了|照做了|已经好了)"
        r"[！!？?。.…~～]*$"
    )

    def classify(self, message: str, previous: Optional[dict]) -> dict:
        text = " ".join((message or "").strip().split())
        topics = chinese_text_analyzer.extract_topics(text, max_topics=4)
        previous_key = (previous or {}).get("topic_key")
        previous_label = (previous or {}).get("topic_label")

        if not previous:
            label = topics[0] if topics else "日常聊天"
            return self._result(ConversationTransition.NEW, label, topics, None)

        explicit_switch = self._explicit_switch.search(text)
        contrast = self._contrast.search(text)
        continuation = self._continuation.search(text)
        supplement = self._supplement.search(text)
        reference = self._reference.search(text)
        depends_on_previous = bool(
            continuation
            or self._elliptical.search(text)
            or self._dependent_reply.fullmatch(text)
            or self._action_confirmation.fullmatch(text)
            or reference
        )
        previous_still_present = previous_key in {
            self._topic_key(topic) for topic in topics
        }

        if explicit_switch and topics:
            label = topics[0]
            transition = ConversationTransition.SWITCH
        elif contrast:
            label = previous_label
            transition = ConversationTransition.CONTRAST
        elif supplement or reference:
            label = previous_label
            transition = ConversationTransition.SUPPLEMENT
        elif (
            continuation
            or self._elliptical.search(text)
            or self._dependent_reply.fullmatch(text)
            or self._action_confirmation.fullmatch(text)
            or not topics
        ):
            label = previous_label
            transition = ConversationTransition.CONTINUATION
        elif previous_still_present:
            label = previous_label
            transition = ConversationTransition.CONTINUATION
        else:
            label = topics[0]
            transition = ConversationTransition.SWITCH

        resolved_reference = None
        if reference:
            resolved_reference = (
                f"“{reference.group(1)}”承接上一片段中“{previous_label}”相关的人物或对象；"
                "具体身份未确认，禁止自行补全"
            )
        elif self._action_confirmation.fullmatch(text):
            resolved_reference = (
                "当前短句是在确认已完成上一轮主播提出的动作或互动；"
                "应按配合主播的回应理解，不能误判为对主播下达指令"
            )
        elif transition in {
            ConversationTransition.CONTINUATION,
            ConversationTransition.CONTRAST,
            ConversationTransition.SUPPLEMENT,
        }:
            resolved_reference = f"本轮承接上一片段的话题“{previous_label}”"
        result = self._result(transition, label, topics, resolved_reference)
        result["depends_on_previous"] = depends_on_previous
        return result

    def _result(
        self,
        transition: ConversationTransition,
        label: str,
        topics: list[str],
        resolved_reference: Optional[str],
    ) -> dict:
        normalized_label = label or "日常聊天"
        return {
            "transition": transition.value,
            "topic_key": self._topic_key(normalized_label),
            "topic_label": normalized_label,
            "detected_topics": topics,
            "resolved_reference": resolved_reference,
            "depends_on_previous": False,
        }

    def _topic_key(self, label: str) -> str:
        return re.sub(r"\s+", "", (label or "日常聊天").casefold())[:80]


class LongTermMemoryManager:
    def __init__(self, database=None):
        self._database = database
        self.continuity = ConversationContinuityAnalyzer()

    @property
    def database(self):
        return self._database or db_manager

    def retrieve_for_reply(
        self, identity: Optional[ViewerIdentity], message: str
    ) -> Optional[dict]:
        if not self._can_use(identity):
            return None

        now = datetime.now(timezone.utc)
        self._purge(now)
        recent = self.database.list_account_conversation_fragments(
            identity.account_id, limit=40
        )
        previous = recent[0] if recent else None
        thread = self.continuity.classify(message, previous)

        ranked = sorted(
            recent,
            key=lambda item: self._fragment_retrieval_score(item, thread, now),
            reverse=True,
        )[:settings.memory.retrieval_limit]
        summaries = self.database.list_account_topic_memories(
            identity.account_id, limit=20
        )
        ranked_summaries = sorted(
            summaries,
            key=lambda item: self._topic_retrieval_score(item, thread, now),
            reverse=True,
        )[:3]

        accessed_at = now.isoformat()
        self.database.mark_account_fragments_accessed(
            identity.account_id, [item["id"] for item in ranked], accessed_at
        )
        self.database.mark_account_topics_accessed(
            identity.account_id, [item["id"] for item in ranked_summaries], accessed_at
        )
        return {
            **thread,
            "account_id": identity.account_id,
            "previous_fragment": self._prompt_fragment(previous) if previous else None,
            "recent_fragments": [self._prompt_fragment(item) for item in ranked],
            "topic_summaries": [self._prompt_summary(item) for item in ranked_summaries],
            "evidence_only": True,
        }

    def record_exchange(
        self,
        *,
        identity: Optional[ViewerIdentity],
        danmaku_id: str,
        viewer_message: str,
        reply_data: dict,
        analysis: Any = None,
        retrieval_context: Optional[dict] = None,
    ) -> Optional[dict]:
        if not self._can_use(identity):
            return None
        safe_message = account_memory_policy.prepare_text(viewer_message)
        if safe_message is None:
            return None
        safe_reply, safe_payload = self._safe_reply(reply_data)
        if not safe_reply:
            return None

        now = datetime.now(timezone.utc)
        previous = self.database.list_account_conversation_fragments(
            identity.account_id, limit=1
        )
        thread = self._thread_from_context(retrieval_context) or self.continuity.classify(
            safe_message, previous[0] if previous else None
        )
        importance = self._importance(safe_message, analysis, thread)
        expires_at = now + timedelta(days=settings.memory.retention_days)
        fragment = self.database.insert_account_conversation_fragment({
            "account_id": identity.account_id,
            "session_scope_id": identity.session_scope_id,
            "danmaku_id": danmaku_id or f"memory-{uuid.uuid4()}",
            "nickname": identity.current_nickname,
            "nickname_version": identity.nickname_version or 1,
            "viewer_message": safe_message,
            "streamer_reply": safe_reply,
            "reply_payload": safe_payload,
            "topic_key": thread["topic_key"],
            "topic_label": thread["topic_label"],
            "transition": thread["transition"],
            "resolved_reference": thread.get("resolved_reference"),
            "sentiment": self._sentiment(analysis),
            "importance": importance,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        })
        if fragment:
            self._compact(identity.account_id, now)
            self._purge(now)
        return fragment

    def _can_use(self, identity: Optional[ViewerIdentity]) -> bool:
        if not identity or not identity.is_authenticated:
            return False
        preference = self.database.get_account_memory_preference(identity.account_id)
        return bool(preference["long_term_memory_enabled"])

    def _thread_from_context(self, context: Optional[dict]) -> Optional[dict]:
        required = {"transition", "topic_key", "topic_label"}
        if context and required.issubset(context):
            return {key: context.get(key) for key in (
                "transition", "topic_key", "topic_label", "detected_topics",
                "resolved_reference", "depends_on_previous",
            )}
        return None

    def _safe_reply(self, reply_data: dict) -> tuple[str, dict]:
        safe_sentences = []
        for sentence in (reply_data or {}).get("sentences", []):
            safe_text = account_memory_policy.prepare_text(str(sentence.get("text", "")))
            if safe_text:
                safe_sentences.append({
                    "emotion": str(sentence.get("emotion", ""))[:30],
                    "text": safe_text,
                })
        payload = {
            "emotions": [item["emotion"] for item in safe_sentences],
            "sentences": safe_sentences,
        }
        return " ".join(item["text"] for item in safe_sentences), payload

    def _importance(self, message: str, analysis: Any, thread: dict) -> float:
        intensity = float(getattr(analysis, "content_intensity", 0.4) or 0.4)
        relevance = float(getattr(analysis, "context_relevance", 0.5) or 0.5)
        score = 0.22 + min(len(message) / 240.0, 0.18)
        score += min(max(intensity, 0.0), 1.0) * 0.20
        score += min(max(relevance, 0.0), 1.0) * 0.15
        if any(mark in message for mark in "?？"):
            score += 0.08
        if thread["transition"] in {
            ConversationTransition.CONTRAST.value,
            ConversationTransition.CONTINUATION.value,
            ConversationTransition.SUPPLEMENT.value,
        }:
            score += 0.10
        return round(max(0.05, min(1.0, score)), 4)

    def _sentiment(self, analysis: Any) -> float:
        if analysis is None:
            return 0.0
        tone = getattr(analysis, "emotional_tone", "neutral")
        intensity = min(1.0, max(0.0, float(
            getattr(analysis, "content_intensity", 0.5) or 0.5
        )))
        return intensity if tone == "positive" else -intensity if tone == "negative" else 0.0

    def _fragment_retrieval_score(self, item: dict, thread: dict, now: datetime) -> float:
        score = float(item.get("importance", 0.5)) * 2.0
        if item.get("topic_key") == thread["topic_key"]:
            score += 3.0
        score += self._recency_score(item.get("created_at"), now)
        score += min(int(item.get("access_count", 0)), 5) * 0.04
        return score

    def _topic_retrieval_score(self, item: dict, thread: dict, now: datetime) -> float:
        score = float(item.get("importance", 0.5)) * 1.5
        if item.get("topic_key") == thread["topic_key"]:
            score += 3.0
        return score + self._recency_score(item.get("last_seen_at"), now)

    def _recency_score(self, timestamp: Optional[str], now: datetime) -> float:
        parsed = self._parse_time(timestamp) or now
        age_days = max(0.0, (now - parsed).total_seconds() / 86400.0)
        half_life = settings.memory.importance_half_life_days
        return math.exp(-math.log(2) * age_days / half_life)

    def _prompt_fragment(self, item: dict) -> dict:
        return {
            "id": item["id"],
            "topic": item["topic_label"],
            "transition": item["transition"],
            "viewer_message": item["viewer_message"],
            "streamer_reply": item["streamer_reply"],
            "created_at": item["created_at"],
            "nickname_version": item["nickname_version"],
        }

    def _prompt_summary(self, item: dict) -> dict:
        return {
            "topic": item["topic_label"],
            "summary": item["summary"],
            "source_count": item["source_count"],
            "last_seen_at": item["last_seen_at"],
        }

    def _compact(self, account_id: str, now: datetime) -> None:
        active = self.database.list_account_conversation_fragments(
            account_id, limit=200
        )
        threshold = settings.memory.compact_after_fragments
        if len(active) <= threshold:
            return
        candidates = active[settings.memory.recent_fragment_limit:]
        grouped: dict[str, list[dict]] = defaultdict(list)
        for fragment in reversed(candidates):
            grouped[fragment["topic_key"]].append(fragment)

        for topic_key, fragments in grouped.items():
            existing = self.database.get_account_topic_memory(account_id, topic_key)
            additions = [
                self._summary_line(fragment) for fragment in fragments
            ]
            parts = ([existing["summary"]] if existing else []) + additions
            summary = self._bounded_summary(parts)
            source_count = int((existing or {}).get("source_count", 0)) + len(fragments)
            importance = max(
                [float((existing or {}).get("importance", 0.0))]
                + [float(item["importance"]) for item in fragments]
            )
            self.database.upsert_account_topic_memory({
                "account_id": account_id,
                "topic_key": topic_key,
                "topic_label": fragments[-1]["topic_label"],
                "summary": summary,
                "source_count": source_count,
                "importance": importance,
                "first_seen_at": (existing or {}).get(
                    "first_seen_at", fragments[0]["created_at"]
                ),
                "last_seen_at": fragments[-1]["created_at"],
                "expires_at": (now + timedelta(days=settings.memory.retention_days)).isoformat(),
            })
        self.database.archive_account_fragments(
            account_id, [item["id"] for item in candidates]
        )

    def _summary_line(self, fragment: dict) -> str:
        date = fragment["created_at"][:10]
        viewer = fragment["viewer_message"][:120]
        streamer = fragment["streamer_reply"][:120]
        return f"{date} 观众提到：{viewer}；主播回应：{streamer}"

    def _bounded_summary(self, parts: list[str]) -> str:
        max_chars = settings.memory.summary_max_chars
        lines = [line for part in parts for line in part.splitlines() if line.strip()]
        while len("\n".join(lines)) > max_chars and len(lines) > 1:
            lines.pop(0)
        return "\n".join(lines)[-max_chars:]

    def _purge(self, now: datetime) -> None:
        result = self.database.purge_expired_account_long_term_memory(
            now.isoformat(), settings.memory.max_archived_fragments
        )
        if any(result.values()):
            logger.info("账号长期记忆清理完成: %s", result)

    def _parse_time(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


long_term_memory_manager = LongTermMemoryManager()
