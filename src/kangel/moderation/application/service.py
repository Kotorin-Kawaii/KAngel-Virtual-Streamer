"""主播管理协调服务：LLM 建议、确定性策略和 SQLite 状态。"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from config import settings
from kangel.audience.domain.identity import ViewerIdentity, ViewerIdentityType
from kangel.infrastructure.database import db_manager
from kangel.shared.logging import logger

from kangel.moderation.application.analyzer import moderation_analyzer
from kangel.moderation.application.models import (
    BehaviorAssessment, ModerationContext, ModerationDecision, message_digest,
)


class ModerationService:
    """LLM 只提供证据；该服务拥有最终动作和状态提交权限。"""

    def __init__(self, database=None):
        self.database = database or db_manager
        self._metrics = {
            "analysis_started": 0,
            "analysis_completed": 0,
            "analysis_failed": 0,
            "analysis_dropped": 0,
            "action_none": 0,
            "action_warning": 0,
            "action_timeout": 0,
            "action_admin_review": 0,
            "muted_rejected": 0,
            "reply_fallback": 0,
        }
        self._last_retention_cleanup = 0.0

    @staticmethod
    def subject_key(identity: Optional[ViewerIdentity], connection_id: str) -> tuple[str, str, Optional[str]]:
        if identity and identity.identity_type == ViewerIdentityType.AUTHENTICATED:
            return identity.subject_id, "authenticated", identity.account_id
        return (identity.subject_id if identity else f"guest:{connection_id}"), "guest", None

    def status(self, subject_key: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        self._maybe_cleanup_retention(now)
        self.database.recover_stale_moderation_actions(
            now=now.isoformat(),
            stale_after_seconds=settings.moderation.reservation_ttl_seconds,
        )
        state = self.database.decay_user_behavior_state(
            subject_key, now=now.isoformat(), decay_per_minute=settings.moderation.decay_per_minute
        ) or self.database.get_user_behavior_state(subject_key)
        if not state:
            return {
                "muted": False, "mute_until": None, "pending": False,
                "admin_review_required": False, "retry_after_seconds": 0,
            }
        mute_until = self._parse_time(state.get("mute_until"))
        pending = bool(state.get("pending_action"))
        muted = pending or bool(mute_until and mute_until > now)
        retry_after = max(0, math.ceil((mute_until - now).total_seconds())) if mute_until and mute_until > now else 0
        return {
            "muted": muted,
            "mute_until": state.get("mute_until"),
            "pending": pending,
            "admin_review_required": bool(state.get("admin_review_required")),
            "retry_after_seconds": retry_after,
        }

    def recent_context(self, subject_key: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        now = datetime.now(timezone.utc)
        state = self.database.decay_user_behavior_state(
            subject_key, now=now.isoformat(), decay_per_minute=settings.moderation.decay_per_minute
        ) or self.database.get_user_behavior_state(subject_key) or {}
        cutoff = (now - timedelta(minutes=settings.moderation.violation_window_minutes)).isoformat()
        recent = self.database.get_recent_moderation_actions(
            subject_key, cutoff, settings.moderation.recent_message_limit
        )
        public_state = {
            "toxicity_score": round(float(state.get("toxicity_score", 0.0)), 3),
            "warning_count": int(state.get("warning_count", 0)),
            "violation_count": int(state.get("violation_count", 0)),
            "recent_violation_count": sum(1 for item in recent if item.get("action") != "none"),
            "last_violation_at": state.get("last_violation_at"),
            "muted": self.status(subject_key)["muted"],
        }
        return public_state, [
            {
                "action": item.get("action"),
                "severity": round(float(item.get("severity", 0.0)), 3),
                "attack_type": item.get("attack_type", "none"),
                "created_at": item.get("created_at"),
            }
            for item in recent
        ]

    async def analyze_and_decide(
        self, *, danmaku_id: str, message: str, nickname: str,
        identity: Optional[ViewerIdentity], connection_id: str,
        context: dict[str, Any],
    ) -> Optional[ModerationDecision]:
        """异步执行一次 moderation；模型失败时返回 None（普通弹幕放行）。"""
        if not settings.moderation.enabled or not settings.moderation.analysis_enabled:
            return None
        subject_key, identity_type, account_id = self.subject_key(identity, connection_id)
        state, recent = self.recent_context(subject_key)
        moderation_context = ModerationContext(
            nickname=nickname[:100], message=message[:500],
            recent_behavior=recent, behavior_state=state,
            viewer_relationship=context.get("viewer_relationship") or {},
            direct_context=context.get("direct_context") or {},
            stream_context=context.get("stream_context") or {},
            persona_state=context.get("persona_state") or {},
            internal_state=context.get("internal_state") or {},
        )
        self._metrics["analysis_started"] += 1
        try:
            assessment = await moderation_analyzer.analyze(moderation_context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._metrics["analysis_failed"] += 1
            logger.warning("主播管理 LLM 分析失败，普通弹幕放行: %s", exc)
            return None
        self._metrics["analysis_completed"] += 1
        decision = self._determine(
            assessment=assessment, message=message, state=state,
            recent=recent, context=context,
        )
        moderation_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        row = self.database.upsert_moderation_assessment(
            moderation_id=moderation_id, danmaku_id=danmaku_id,
            subject_key=subject_key, identity_type=identity_type,
            account_id=account_id,
            stream_session_id=context.get("stream_session_id"),
            action=decision["action"], severity=decision["severity"],
            toxicity=assessment.toxicity, confidence=assessment.confidence,
            attack_type=assessment.attack_type, reason_code=decision["reason_code"],
            mute_until=decision["mute_until"],
            message_digest=message_digest(message), now=now,
        )
        self._metrics[f"action_{row['action']}"] = self._metrics.get(f"action_{row['action']}", 0) + 1
        return ModerationDecision(
            moderation_id=row["moderation_id"], danmaku_id=danmaku_id,
            subject_key=subject_key, action=row["action"],
            toxicity=float(row["toxicity"]), confidence=float(row["confidence"]),
            severity=float(row["severity"]), attack_type=row["attack_type"],
            reason_code=row["reason_code"], mute_until=row.get("mute_until"),
            reserved=row.get("status") == "reserved",
        )

    def _determine(
        self, *, assessment: BehaviorAssessment, message: str,
        state: dict[str, Any], recent: list[dict[str, Any]], context: dict[str, Any],
    ) -> dict[str, Any]:
        """把 LLM 证据投影为确定性动作；不接受模型的时间或身份字段。"""
        text = (message or "").casefold()
        hard = any(term.casefold() in text for term in settings.moderation.hard_violation_terms if term.strip())
        score = max(float(assessment.toxicity), self._severity_floor(assessment.severity))
        recent_count = sum(1 for item in recent if item.get("action") != "none")
        repeated_bonus = min(0.20, max(0, recent_count - 1) * 0.06)
        score = min(1.0, score + repeated_bonus)
        # 低置信模型建议不能单独升级为 timeout/admin_review；硬词和
        # threat/doxxing 仍由后端硬规则直接接管。
        if assessment.confidence < 0.45 and not hard and assessment.attack_type not in {"threat", "doxxing"}:
            score = min(score, 0.45)

        relationship = context.get("viewer_relationship") or {}
        mild = assessment.attack_type in {"none", "personal_attack", "other"} and score < 0.8
        if mild and float(relationship.get("familiarity", 0.0) or 0.0) >= 0.7:
            trust = float(relationship.get("trust", 0.0) or 0.0)
            relief = min(settings.moderation.relation_relief_max, max(0.0, trust - 0.5) * 0.4)
            score = max(0.0, score * (1.0 - relief))

        profile = "special_event" if (context.get("stream_context") or {}).get("special_date_theme") else "default"
        thresholds = settings.moderation.stream_profiles.get(profile, settings.moderation.stream_profiles["default"])
        if hard or assessment.attack_type in {"threat", "doxxing"}:
            action = "admin_review"
            score = max(score, float(thresholds.get("admin_review", settings.moderation.admin_review_threshold)))
            reason = "hard_violation" if hard else assessment.attack_type
        elif score >= float(thresholds.get("admin_review", settings.moderation.admin_review_threshold)):
            action, reason = "admin_review", assessment.reason_code or "high_toxicity"
        elif score >= float(thresholds.get("timeout", settings.moderation.timeout_threshold)):
            action, reason = "timeout", assessment.reason_code or "repeated_attack"
        elif score >= float(thresholds.get("warning", settings.moderation.warning_threshold)):
            action, reason = "warning", assessment.reason_code or "boundary_warning"
        else:
            action, reason = "none", "none"

        # 模型明确要求低级动作时允许它在对应阈值内提升动作，不允许降低硬规则。
        if action == "none" and assessment.proposed_action == "warning" and score >= .45:
            action, reason = "warning", assessment.reason_code or "model_warning"
        if action == "warning" and assessment.proposed_action == "timeout" and recent_count >= 2:
            action, reason = "timeout", assessment.reason_code or "repeated_attack"

        mute_until = None
        if action in {"timeout", "admin_review"}:
            seconds = settings.moderation.max_timeout_seconds if action == "admin_review" else settings.moderation.timeout_seconds
            mute_until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
        return {"action": action, "severity": score, "reason_code": reason, "mute_until": mute_until}

    @staticmethod
    def _severity_floor(value: str) -> float:
        return {"none": 0.0, "warning": .60, "timeout": .80, "admin_review": .95}.get(value, 0.0)

    def is_blocked(self, subject_key: str) -> dict[str, Any]:
        result = self.status(subject_key)
        if result["muted"]:
            self._metrics["muted_rejected"] += 1
        return result

    def complete_action(self, moderation_id: str, reply_payload: Optional[dict]) -> bool:
        row = self.database.get_moderation_action(moderation_id) if hasattr(self.database, "get_moderation_action") else None
        mute_until = row.get("mute_until") if row else None
        return self.database.complete_moderation_action(
            moderation_id, reply_payload=reply_payload,
            mute_until=mute_until,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    def release_action(self, moderation_id: str) -> bool:
        return self.database.release_moderation_action(
            moderation_id, datetime.now(timezone.utc).isoformat()
        )

    def forget_guest(self, subject_key: str) -> None:
        self.database.clear_guest_behavior_state(subject_key)

    def get_stats(self) -> dict[str, Any]:
        return {**self._metrics, "database": self.database.get_moderation_stats()}

    def record_analysis_dropped(self) -> None:
        """后台队列满时记录低基数指标；不影响原始弹幕放行。"""
        self._metrics["analysis_dropped"] += 1

    def record_reply_fallback(self) -> None:
        self._metrics["reply_fallback"] += 1

    def _maybe_cleanup_retention(self, now: datetime) -> None:
        current = time.monotonic()
        if current - self._last_retention_cleanup < 60:
            return
        cutoff = now - timedelta(days=settings.moderation.state_retention_days)
        try:
            self.database.purge_moderation_history(cutoff.isoformat())
            self._last_retention_cleanup = current
        except Exception as exc:
            # 清理失败不能影响当前消息的安全判断。
            logger.warning("主播管理历史清理失败，继续使用现有状态: %s", exc)

    @staticmethod
    def _parse_time(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


moderation_service = ModerationService()
