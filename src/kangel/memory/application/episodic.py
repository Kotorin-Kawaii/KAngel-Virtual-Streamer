"""P24 主播情景记忆：候选采集、下播反思、受限召回与异步消费。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config import settings
from kangel.audience.domain.identity import ViewerIdentity
from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
from kangel.infrastructure.database import DatabaseManager, db_manager
from kangel.integrations.ai.service import AIService, ai_service
from kangel.shared.logging import logger
from .runtime import account_memory_policy


EPISODIC_MEMORY_VERSION = "stream_episodic_memory_v1"
_EVENT_TYPES = frozenset({
    "personal_disclosure", "affection_or_support", "shared_joke_or_callback",
    "promise_or_open_thread", "sc_highlight", "boundary_incident",
    "room_incident", "activity_milestone",
})
_TRIGGERS = {
    "distress_share": "personal_disclosure",
    "affirmation": "affection_or_support",
    "cooperative_response": "shared_joke_or_callback",
    "boundary_challenge": "boundary_incident",
    "pressure_or_demand": "boundary_incident",
    "activity_progress": "activity_milestone",
}


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _signed(value: Any) -> float:
    try:
        return max(-1.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("情景记忆模型输出必须是 JSON 对象")
    return value


class EpisodicMemoryProcessingError(RuntimeError):
    """Auditable, bounded failure emitted by one P24 provider execution."""

    def __init__(self, code: str, detail: str, *, retryable: bool = True):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable


def _classify_processing_error(exc: BaseException) -> EpisodicMemoryProcessingError:
    if isinstance(exc, EpisodicMemoryProcessingError):
        return exc
    if isinstance(exc, asyncio.TimeoutError):
        return EpisodicMemoryProcessingError("provider_timeout", "session memory provider timeout")
    if isinstance(exc, json.JSONDecodeError):
        return EpisodicMemoryProcessingError("invalid_json", "session memory response was not valid JSON")
    text = " ".join(str(exc).split())
    folded = text.casefold()
    if "http error" in folded or "status code" in folded or "http 4" in folded or "http 5" in folded:
        return EpisodicMemoryProcessingError("provider_http_error", text)
    if "缺少 message.content" in text or "缺少 choices" in text or "empty response" in folded:
        return EpisodicMemoryProcessingError("provider_empty_response", text)
    if "必须" in text or "必须为" in text or "schema" in folded or "字段" in text:
        return EpisodicMemoryProcessingError("schema_validation_failed", text)
    return EpisodicMemoryProcessingError("unknown_generation_error", text or exc.__class__.__name__)


class EpisodicMemoryManager:
    """实时线程只写候选；AI 总结在独立、可恢复的低优先级任务中执行。"""

    def __init__(self, database: Optional[DatabaseManager] = None):
        self.database = database or db_manager
        self._last_purge_at = 0.0

    @staticmethod
    def _ai_timeout() -> int:
        return min(
            settings.episodic_memory.ai_timeout_seconds,
            settings.ai.session_memory_timeout,
        )

    @staticmethod
    def _identity_scope(identity: Optional[ViewerIdentity], database: DatabaseManager) -> tuple[str, Optional[str], str]:
        if identity and identity.is_authenticated:
            preference = database.get_account_memory_preference(identity.account_id)
            if preference.get("long_term_memory_enabled"):
                return "account", identity.account_id, "authenticated"
        return "room", None, "guest"

    def capture_reply(
        self, *, stream_session_id: Optional[str], danmaku_id: str, message: str,
        identity: Optional[ViewerIdentity], analysis: Any = None,
        is_sc: bool = False,
    ) -> Optional[dict]:
        if not settings.episodic_memory.enabled or not stream_session_id or not danmaku_id:
            return None
        scope, account_id, identity_type = self._identity_scope(identity, self.database)
        appraisal = getattr(analysis, "appraisal", None)
        appraisal_data = appraisal.to_dict() if appraisal and hasattr(appraisal, "to_dict") else {}
        trigger = str(appraisal_data.get("trigger_class", "neutral_interaction"))
        confidence = float(appraisal_data.get("confidence", 0.0) or 0.0)
        novelty = abs(float(appraisal_data.get("novelty", 0.0) or 0.0))
        importance = 0.0
        fragment_transition = ""
        fragment_reference = ""
        source_type = "reply_record"
        source_id = danmaku_id
        topic = ""
        if account_id:
            fragment = self.database.get_account_conversation_fragment_by_danmaku(account_id, danmaku_id)
            if fragment:
                importance = float(fragment.get("importance", 0.0) or 0.0)
                topic = fragment.get("topic_label", "")
                fragment_transition = str(fragment.get("transition", ""))
                fragment_reference = str(fragment.get("resolved_reference", "") or "")
                if importance >= settings.episodic_memory.candidate_min_importance:
                    source_type, source_id = "account_fragment", str(fragment["id"])
        if is_sc:
            event_type = "sc_highlight"
            salience = 1.0
            source_type, source_id = "sc", danmaku_id
        else:
            importance_eligible = importance >= settings.episodic_memory.candidate_min_importance
            appraisal_eligible = (
                confidence >= settings.episodic_memory.appraisal_min_confidence
                and (
                    trigger in {"distress_share", "boundary_challenge", "pressure_or_demand"}
                    or novelty >= settings.episodic_memory.appraisal_min_novelty
                )
            )
            event_type = _TRIGGERS.get(trigger, "")
            # 事件类别本身不能绕过准入门槛；只有高重要性账号片段，或
            # 达到置信度且具有痛苦/边界/新颖度证据的评价，才进入候选池。
            if event_type and not importance_eligible and not appraisal_eligible:
                event_type = ""
            salience = max(importance, confidence * 0.55 + novelty * 0.45)
            if not event_type and importance_eligible:
                tone = str(getattr(analysis, "emotional_tone", "neutral") or "neutral")
                event_type = (
                    "personal_disclosure" if tone in {"negative", "mixed"}
                    else "affection_or_support" if tone == "positive"
                    else "shared_joke_or_callback"
                )
            if event_type != "boundary_incident" and (fragment_reference or fragment_transition in {"continuation", "supplement"}):
                event_type = "promise_or_open_thread"
            if not event_type and appraisal_eligible:
                event_type = "room_incident" if scope == "room" else "personal_disclosure"
            if not event_type or not (importance_eligible or appraisal_eligible):
                return None
        now = datetime.now(timezone.utc).isoformat()
        return self.database.insert_stream_memory_candidate({
            "candidate_id": uuid.uuid4().hex,
            "stream_session_id": stream_session_id,
            "scope": "account" if account_id else "room",
            "identity_type": identity_type,
            "account_id": account_id,
            "event_type": event_type,
            "source_type": source_type,
            "source_id": source_id,
            "topic": topic,
            "salience": min(1.0, max(0.0, salience)),
            "valence": float(getattr(analysis, "sentiment", 0.0) or 0.0) if analysis else 0.0,
            "appraisal": {
                "trigger_class": trigger, "confidence": confidence, "novelty": novelty,
                "importance": importance,
                "reward_or_threat": _signed(appraisal_data.get("reward_or_threat")),
                "affiliation": _signed(appraisal_data.get("affiliation")),
                "agency_or_pressure": _signed(appraisal_data.get("agency_or_pressure")),
            },
            "occurred_at": now,
            "created_at": now,
        })

    def capture_moderation(
        self, *, stream_session_id: Optional[str], moderation_id: str,
        account_id: Optional[str], identity_type: str, action: str,
        occurred_at: Optional[str] = None,
    ) -> Optional[dict]:
        if not settings.episodic_memory.enabled or not stream_session_id or action not in {"warning", "timeout", "admin_review"}:
            return None
        if account_id:
            preference = self.database.get_account_memory_preference(account_id)
            if not preference.get("long_term_memory_enabled"):
                account_id = None
                identity_type = "guest"
        now = occurred_at or datetime.now(timezone.utc).isoformat()
        return self.database.insert_stream_memory_candidate({
            "candidate_id": uuid.uuid4().hex,
            "stream_session_id": stream_session_id,
            "scope": "account" if account_id else "room",
            "identity_type": identity_type,
            "account_id": account_id,
            "event_type": "boundary_incident",
            "source_type": "moderation",
            "source_id": moderation_id,
            "topic": "直播间边界",
            "salience": 1.0 if action == "admin_review" else 0.8,
            "valence": -1.0,
            "appraisal": {"action": action},
            "occurred_at": now,
            "created_at": now,
        })

    def capture_activity(self, *, stream_session_id: Optional[str], transition: dict) -> Optional[dict]:
        if not settings.episodic_memory.enabled or not stream_session_id or not transition.get("id"):
            return None
        now = str(transition.get("changed_at") or datetime.now(timezone.utc).isoformat())
        return self.database.insert_stream_memory_candidate({
            "candidate_id": uuid.uuid4().hex,
            "stream_session_id": stream_session_id,
            "scope": "room", "identity_type": "system",
            "event_type": "activity_milestone", "source_type": "activity",
            "source_id": str(transition["id"]),
            "topic": transition.get("display_name", "直播活动"),
            "salience": 0.65, "valence": 0.0,
            "appraisal": {"trigger_class": "activity_progress"},
            "occurred_at": now, "created_at": now,
        })

    def freeze_session(self, stream_session_id: str, *, now: Optional[str] = None) -> bool:
        if not settings.episodic_memory.enabled:
            return False
        if self.database.stream_session_is_active(stream_session_id):
            logger.debug("P24 跳过 active 场次冻结: %s", stream_session_id)
            return False
        candidates = self.database.list_stream_memory_candidates(
            stream_session_id, limit=settings.episodic_memory.max_candidates_per_session
        )
        if not candidates:
            return False
        selected: list[dict] = []
        account_counts: dict[str, int] = {}
        for item in candidates:
            account_id = item.get("account_id")
            if account_id:
                count = account_counts.get(account_id, 0)
                if count >= settings.episodic_memory.max_candidates_per_account:
                    continue
                account_counts[account_id] = count + 1
            selected.append(item)
        return self.database.create_stream_memory_task(
            stream_session_id=stream_session_id,
            candidate_ids=[item["candidate_id"] for item in selected],
            created_at=now or datetime.now(timezone.utc).isoformat(),
            source_version=EPISODIC_MEMORY_VERSION,
        )

    def retrieve_for_reply(
        self, identity: Optional[ViewerIdentity], message: str, *, include_reflection: bool = False
    ) -> Optional[dict]:
        if not settings.episodic_memory.enabled:
            return None
        if time.monotonic() - self._last_purge_at >= 60:
            try:
                self.database.purge_expired_episodic_memory(
                    datetime.now(timezone.utc).isoformat()
                )
                self._last_purge_at = time.monotonic()
            except Exception as exc:
                logger.debug("P24 过期情景记忆清理失败: %s", exc)
        account_memories: list[dict] = []
        if identity and identity.is_authenticated:
            preference = self.database.get_account_memory_preference(identity.account_id)
            if preference.get("long_term_memory_enabled"):
                account_pool = self.database.list_account_episodic_memories(
                    identity.account_id, limit=max(50, settings.episodic_memory.retrieval_account_limit)
                )
                # 账号身份本身不是相关性证明；只有当前消息明确触及记忆话题
                # 才允许注入，避免“同一个人出现”就机械复述前场数据库。
                account_memories = [
                    item for item in self._rank_related(account_pool, message)
                    if self._topic_matches(item, message)
                ][:settings.episodic_memory.retrieval_account_limit]
        topic = " ".join(str(message or "").split())[:120]
        room_pool = self.database.list_room_episodic_memories(
            topic="", limit=max(50, settings.episodic_memory.retrieval_room_limit)
        ) if topic else []
        room_memories = [
            item for item in self._rank_related(room_pool, topic)
            if not topic or self._topic_matches(item, topic)
        ][:settings.episodic_memory.retrieval_room_limit]
        reflection = self.database.get_latest_stream_reflection() if include_reflection else None
        if not account_memories and not room_memories and not reflection:
            return None
        payload = {
            "account_memories": [self._prompt_memory(item) for item in account_memories],
            "room_memories": [self._prompt_memory(item) for item in room_memories],
            "stream_reflection": self._prompt_reflection(reflection) if reflection else None,
            "evidence_only": True,
        }
        return self._bound_payload(payload)

    @staticmethod
    def _topic_matches(item: dict, message: str) -> bool:
        topic = "".join(str(item.get("topic", "")).casefold().split())
        text = "".join(str(message or "").casefold().split())
        return bool(topic and len(topic) >= 2 and topic in text)

    @classmethod
    def _rank_related(cls, items: list[dict], message: str) -> list[dict]:
        return sorted(
            items,
            key=lambda item: (
                1 if cls._topic_matches(item, message) else 0,
                float(item.get("salience", 0.0) or 0.0),
                str(item.get("occurred_at", "")),
            ),
            reverse=True,
        )

    @staticmethod
    def _prompt_memory(item: dict) -> dict:
        # 召回提示比 API 导出更严格，避免两条长尾记忆稀释当前语义。
        return {
            "event_type": item.get("event_type"),
            "topic": _clip(item.get("topic"), 40),
            "summary": _clip(item.get("summary"), 120),
            "why_notable": _clip(item.get("why_notable"), 60),
            "follow_up_hint": _clip(item.get("follow_up_hint"), 70),
            "occurred_at": item.get("occurred_at"),
        }

    @staticmethod
    def _prompt_reflection(item: dict) -> dict:
        return {
            "summary": _clip(item.get("summary"), 240),
            "emotional_residue": _clip(item.get("emotional_residue"), 100),
            "open_callbacks": [_clip(value, 100) for value in item.get("open_callbacks", [])[:3]],
        }

    def _bound_payload(self, payload: dict) -> dict:
        limit = settings.episodic_memory.retrieval_prompt_chars
        while len(json.dumps(payload, ensure_ascii=False)) > limit:
            if payload.get("room_memories"):
                payload["room_memories"].pop()
            elif payload.get("stream_reflection"):
                payload["stream_reflection"] = None
            elif payload.get("account_memories"):
                payload["account_memories"].pop()
            else:
                break
        return payload

    def get_reflection_context(self, *, exclude_stream_session_id: Optional[str] = None) -> Optional[dict]:
        """提供给开播/主动待机等非逐条回复场景的低密度前场余韵。"""
        if not settings.episodic_memory.enabled:
            return None
        reflection = self.database.get_latest_stream_reflection(
            exclude_stream_session_id=exclude_stream_session_id
        )
        if not reflection:
            return None
        return self._bound_payload({
            "stream_reflection": self._prompt_reflection(reflection),
            "evidence_only": True,
        })

    def get_stats(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.episodic_memory.enabled),
            "ai_enabled": bool(settings.episodic_memory.ai_enabled),
            **self.database.get_stream_episodic_memory_stats(),
        }

    async def process_once(self, ai_client: AIService = ai_service) -> bool:
        if not settings.episodic_memory.enabled or not settings.episodic_memory.ai_enabled:
            return False
        from kangel.integrations.superchat.service import sc_service
        gate = ai_reply_work_gate.snapshot()
        if gate["active"] or gate["waiting"] or await asyncio.to_thread(sc_service.has_active_work):
            return False
        # Startup and every worker wake perform a bounded, idempotent
        # reconciliation.  It repairs old terminal failures and task/candidate
        # drift without consuming the active session.
        try:
            reconciliation = await asyncio.to_thread(
                self.database.reconcile_stream_memory_lifecycle,
                now=datetime.now(timezone.utc).isoformat(),
                include_orphans=True,
            )
            if reconciliation.get("reopened_tasks") or reconciliation.get("created_tasks"):
                logger.info("P24 recovery reconciliation: %s", reconciliation)
        except Exception as exc:
            logger.warning("P24 recovery reconciliation failed: %s", exc)
        now = datetime.now(timezone.utc).isoformat()
        item = await asyncio.to_thread(
            self.database.claim_next_stream_memory_task,
            lease_seconds=settings.episodic_memory.processing_lease_seconds,
            now=now,
        )
        if not item:
            return False
        session_id = item["stream_session_id"]
        claim_token = item.get("claim_token")
        lease = await ai_reply_work_gate.acquire(
            limit=settings.rate_limit.ai_reply_concurrency,
            max_waiters=0,
            wait_timeout=0.05,
        )
        if lease is None:
            await asyncio.to_thread(
                self.database.release_stream_memory_task,
                session_id, datetime.now(timezone.utc).isoformat(), claim_token,
            )
            return False
        try:
            inputs = await asyncio.to_thread(
                self.database.get_stream_memory_candidate_inputs, item["candidate_ids"]
            )
            memories, reflection = await asyncio.wait_for(
                self._summarize(inputs, ai_client),
                timeout=self._ai_timeout(),
            )
            memories = self._validate_memories(memories, inputs)
            reflection = self._validate_reflection(reflection, memories, inputs)
            final_batch = not await asyncio.to_thread(
                self.database.stream_memory_task_has_pending_candidates, session_id
            )
            discarded_reasons = {
                str(item["candidate_id"]): "source_missing"
                for item in inputs
                if item.get("source_missing") and not item.get("source")
            }
            committed = await asyncio.to_thread(
                self.database.complete_stream_memory_batch,
                stream_session_id=session_id, claim_token=claim_token,
                memories=memories, reflection=reflection,
                now=datetime.now(timezone.utc).isoformat(), final_batch=final_batch,
                discarded_reasons=discarded_reasons,
            )
            if not committed:
                logger.info(
                    "P24 memory batch commit skipped because execution lease was superseded: session=%s execution=%s",
                    session_id, claim_token or "legacy",
                )
                return True
            logger.info(
                "P24 memory batch completed: session=%s execution=%s batch=%s candidates=%d memories=%d final=%s",
                session_id, claim_token or "legacy", item.get("batch_index", 0),
                len(item["candidate_ids"]), len(memories), final_batch,
            )
            return True
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.database.release_stream_memory_task, session_id,
                datetime.now(timezone.utc).isoformat(), claim_token,
            )
            raise
        except Exception as exc:
            failure = _classify_processing_error(exc)
            logger.warning(
                "P24 memory batch failed: session=%s execution=%s batch=%s code=%s detail=%s",
                session_id, claim_token or "legacy", item.get("batch_index", 0),
                failure.code, failure.detail[:160],
            )
            await asyncio.to_thread(
                self.database.fail_stream_memory_task,
                stream_session_id=session_id, error_code=failure.code,
                max_attempts=settings.episodic_memory.max_attempts,
                now=datetime.now(timezone.utc).isoformat(),
                error_detail=failure.detail, retryable=failure.retryable,
                claim_token=claim_token,
            )
            return True
        finally:
            await lease.release()

    async def _summarize(self, inputs: list[dict], ai_client: AIService) -> tuple[list[dict], Optional[dict]]:
        evidence = []
        for item in inputs[:settings.episodic_memory.max_candidates_per_session]:
            source = item.get("source") or {}
            safe_source = {
                "kind": item.get("source_type"), "topic": item.get("topic"),
                "source_missing": not bool(source),
            }
            if item.get("source_type") == "account_fragment":
                safe_source.update({"viewer_message": _clip(source.get("viewer_message"), settings.episodic_memory.source_excerpt_chars), "streamer_reply": _clip(source.get("streamer_reply"), settings.episodic_memory.source_excerpt_chars)})
            elif item.get("source_type") == "reply_record":
                safe_source.update({"message": _clip(source.get("danmaku_message"), settings.episodic_memory.source_excerpt_chars), "reply": _clip(source.get("ai_reply"), settings.episodic_memory.source_excerpt_chars)})
            elif item.get("source_type") == "sc":
                safe_source.update({"content": _clip(source.get("content"), settings.episodic_memory.source_excerpt_chars), "completed": bool(source.get("completed_at"))})
            elif item.get("source_type") == "moderation":
                safe_source.update({
                    "action": source.get("action") if source else item.get("appraisal", {}).get("action"),
                    "attack_type": source.get("attack_type") if source else None,
                    "severity": source.get("severity") if source else None,
                })
            elif item.get("source_type") == "activity":
                safe_source.update({"activity": _clip(source.get("display_name"), 80), "object": _clip(source.get("object_name"), 80)})
            evidence.append({
                "candidate_id": item["candidate_id"], "event_type": item["event_type"],
                "scope": item["scope"], "topic": item["topic"],
                "salience": item["salience"], "appraisal": item.get("appraisal", {}),
                "occurred_at": item["occurred_at"], "evidence": safe_source,
            })
        messages = [
            {"role": "system", "content": (
                "你是虚拟主播的下播情景记忆整理器。只使用候选证据，输出严格 JSON，不输出思维链。"
                "不得编造人物关系、身份、处罚原因或未提供的事实；不得输出账号 ID、昵称、IP、完整原文。"
                "直接互动语义优先于主题和背景。只允许事件类型：" + ",".join(sorted(_EVENT_TYPES))
            )},
            {"role": "user", "content": (
                "候选证据（candidate_id 是内部引用，只能引用这些 ID）：\n"
                + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                + "\n返回：{\"memories\":[{\"candidate_ids\":[...],\"event_type\":\"...\",\"summary\":\"不超过120字\",\"why_notable\":\"不超过100字\",\"emotional_mark\":\"不超过60字\",\"follow_up_hint\":\"不超过100字\",\"topic\":\"不超过60字\",\"salience\":0到1}],"
                "\"reflection\":{\"summary\":\"不超过200字\",\"emotional_residue\":\"不超过80字\",\"open_callbacks\":[]}}。"
            )},
        ]
        response = await ai_client.run(
            messages=messages, role="session_memory",
            model=settings.ai.session_memory_model or settings.ai.default_model,
            model_mode="role_hint", temperature=0.1,
            response_format={"type": "object"}, timeout=self._ai_timeout(),
        )
        raw_reply = response.get("reply") if isinstance(response, dict) else None
        if not str(raw_reply or "").strip():
            raise EpisodicMemoryProcessingError(
                "provider_empty_response", "session memory provider returned an empty response"
            )
        parsed = _parse_json(str(raw_reply))
        return parsed.get("memories") or [], parsed.get("reflection")

    def _validate_memories(self, values: Any, inputs: list[dict]) -> list[dict]:
        by_id = {item["candidate_id"]: item for item in inputs}
        results = []
        account_counts: dict[str, int] = {}
        if not isinstance(values, list):
            raise ValueError("memories 必须为数组")
        for value in values[:settings.episodic_memory.max_memories_per_session]:
            if not isinstance(value, dict):
                continue
            ids = [str(item) for item in value.get("candidate_ids", []) if str(item) in by_id]
            if not ids:
                continue
            candidates = [by_id[item] for item in ids]
            candidate = max(candidates, key=lambda item: float(item.get("salience", 0.0)))
            account_id = candidate.get("account_id") if candidate.get("scope") == "account" else None
            if account_id:
                if account_counts.get(account_id, 0) >= settings.episodic_memory.max_memories_per_account:
                    continue
                account_counts[account_id] = account_counts.get(account_id, 0) + 1
            event_type = str(value.get("event_type") or candidate.get("event_type"))
            if event_type not in _EVENT_TYPES:
                event_type = candidate.get("event_type") if candidate.get("event_type") in _EVENT_TYPES else "room_incident"
            now = datetime.now(timezone.utc)
            retention = settings.episodic_memory.account_retention_days if account_id else settings.episodic_memory.room_retention_days
            forbidden_terms = set()
            if account_id:
                try:
                    forbidden_terms = {
                        str(item.get("nickname", "")).strip()
                        for item in self.database.list_account_nickname_history(account_id)
                        if len(str(item.get("nickname", "")).strip()) >= 2
                    }
                except Exception:
                    forbidden_terms = set()
            raw_evidence = []
            for source_item in candidates:
                source = source_item.get("source") or {}
                raw_evidence.extend(
                    str(source.get(key, "")) for key in (
                        "viewer_message", "streamer_reply", "message", "reply", "content",
                    ) if len(str(source.get(key, ""))) >= 4
                )
            safe_summary = self._safe_generated(value.get("summary"), account_id, raw_evidence, forbidden_terms)
            safe_why = self._safe_generated(value.get("why_notable"), account_id, raw_evidence, forbidden_terms)
            safe_emotion = self._safe_generated(value.get("emotional_mark"), account_id, raw_evidence, forbidden_terms)
            safe_follow_up = self._safe_generated(value.get("follow_up_hint"), account_id, raw_evidence, forbidden_terms)
            safe_topic = self._safe_generated(
                value.get("topic") or candidate.get("topic"), account_id, [], forbidden_terms
            )
            results.append({
                "memory_id": uuid.uuid4().hex, "scope": "account" if account_id else "room",
                "account_id": account_id, "event_type": event_type,
                "topic": _clip(safe_topic, 120),
                "summary": safe_summary or "这场直播出现了一件值得记住的事。",
                "why_notable": safe_why,
                "emotional_mark": safe_emotion,
                "follow_up_hint": safe_follow_up,
                "salience": max(0.0, min(1.0, float(value.get("salience", candidate.get("salience", 0.0)) or 0.0))),
                "occurred_at": min(item.get("occurred_at", now.isoformat()) for item in candidates),
                "evidence_candidate_ids": ids,
                "expires_at": (now + timedelta(days=retention)).isoformat(),
            })
        return results

    @staticmethod
    def _safe_generated(
        value: Any, account_id: Optional[str], raw_evidence: list[str],
        forbidden_terms: Optional[set[str]] = None,
    ) -> str:
        text = account_memory_policy.prepare_text(_clip(value, 240)) or ""
        if account_id and account_id in text:
            return ""
        if any(raw in text for raw in raw_evidence):
            return ""
        if any(term and term in text for term in (forbidden_terms or set())):
            return ""
        return text

    def _validate_reflection(
        self, value: Any, memories: list[dict], inputs: list[dict]
    ) -> Optional[dict]:
        if not isinstance(value, dict):
            return None
        now = datetime.now(timezone.utc)
        raw_evidence: list[str] = []
        forbidden_terms: set[str] = set()
        for item in inputs:
            source = item.get("source") or {}
            raw_evidence.extend(
                str(source.get(key, "")) for key in (
                    "viewer_message", "streamer_reply", "message", "reply", "content",
                ) if len(str(source.get(key, ""))) >= 4
            )
            account_id = str(item.get("account_id") or "").strip()
            if account_id:
                forbidden_terms.add(account_id)
                try:
                    forbidden_terms.update(
                        str(history.get("nickname", "")).strip()
                        for history in self.database.list_account_nickname_history(account_id)
                        if len(str(history.get("nickname", "")).strip()) >= 2
                    )
                except Exception:
                    pass
        summary = self._safe_generated(value.get("summary"), None, raw_evidence, forbidden_terms)
        residue = self._safe_generated(
            value.get("emotional_residue"), None, raw_evidence, forbidden_terms
        )
        callbacks = [
            self._safe_generated(item, None, raw_evidence, forbidden_terms)
            for item in value.get("open_callbacks", [])[:3]
            if str(item).strip()
        ]
        callbacks = [item for item in callbacks if item]
        if not summary and not residue and not callbacks:
            return None
        return {
            "reflection_id": uuid.uuid4().hex,
            "summary": _clip(summary, 480),
            "emotional_residue": _clip(residue, 180),
            "open_callbacks": [_clip(item, 160) for item in callbacks],
            "notable_event_ids": [item["memory_id"] for item in memories[:6]],
            "expires_at": (now + timedelta(days=settings.episodic_memory.reflection_retention_days)).isoformat(),
        }

    async def run(self) -> None:
        while True:
            try:
                processed = await self.process_once()
                if not processed:
                    await asyncio.sleep(settings.episodic_memory.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("P24 情景记忆消费循环异常")
                await asyncio.sleep(settings.episodic_memory.poll_interval_seconds)


class EpisodicMemoryConsumer:
    def __init__(self, manager: Optional[EpisodicMemoryManager] = None):
        self.manager = manager or episodic_memory_manager
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stats = {"processed": 0, "idle_ticks": 0}

    async def start(self) -> None:
        if self._running or not settings.episodic_memory.enabled or not settings.episodic_memory.ai_enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="stream-episodic-memory")

    async def _run(self) -> None:
        while True:
            try:
                processed = await self.manager.process_once()
                if processed:
                    self._stats["processed"] += 1
                    # 即使有历史 backlog，也让普通回复、SC 和 moderation
                    # 协调器获得下一个事件循环机会。
                    await asyncio.sleep(min(1.0, settings.episodic_memory.poll_interval_seconds))
                else:
                    self._stats["idle_ticks"] += 1
                    await asyncio.sleep(settings.episodic_memory.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("P24 情景记忆消费循环异常")
                await asyncio.sleep(settings.episodic_memory.poll_interval_seconds)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def get_stats(self) -> dict[str, Any]:
        return {"running": self._running, **self._stats}


episodic_memory_manager = EpisodicMemoryManager()
episodic_memory_consumer = EpisodicMemoryConsumer(episodic_memory_manager)


__all__ = [
    "EPISODIC_MEMORY_VERSION", "EpisodicMemoryManager", "EpisodicMemoryConsumer",
    "episodic_memory_manager", "episodic_memory_consumer",
]
