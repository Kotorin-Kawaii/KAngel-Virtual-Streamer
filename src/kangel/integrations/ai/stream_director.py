"""可选 AI Director 适配器；只产生候选，永远不提交事实。"""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import asdict
from typing import Any

from config import settings
from kangel.infrastructure.bounded_work_gate import BoundedWorkGate, ai_reply_work_gate
from kangel.integrations.ai.persona_card import build_system_persona_card
from kangel.integrations.ai.service import AIService, ai_service
from kangel.stream.application.director import (
    ANIMATION_IDS,
    DirectorSignalSnapshot,
    FactMutation,
    PerformanceAction,
    StreamerActionDecision,
    TEMPLATE_FAMILIES,
)


stream_director_work_gate = BoundedWorkGate()
_REASONS = frozenset({
    "ROOM_QUIET", "ROOM_SURGE", "ROOM_SENTIMENT", "EXTEND_DETOUR",
    "RETURN_MAINLINE", "GAME_FATIGUE", "HIGH_STRESS", "ACTIVITY_ALIGNMENT",
    "NO_CHANGE",
})
class AIStreamDirectorCandidate:
    def __init__(self, service: AIService = ai_service):
        self.service = service
        self.last_audit: dict[str, Any] | None = None

    async def decide(
        self, context: dict[str, Any], signals: DirectorSignalSnapshot
    ) -> StreamerActionDecision | None:
        lease = await stream_director_work_gate.acquire(
            limit=1, max_waiters=0, wait_timeout=0.1
        )
        if not lease:
            return None
        started_at = time.perf_counter()
        try:
            from kangel.integrations.superchat.service import sc_service

            reply_gate = ai_reply_work_gate.snapshot()
            if reply_gate["active"] or reply_gate["waiting"] or sc_service.has_active_work():
                self.last_audit = {
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "validated": False,
                    "error_code": "higher_priority_work_active",
                }
                return None
            mainline = context["mainline"]
            activity = context["activity"]
            allowed_beats = [
                {
                    "id": beat.beat_id, "kind": beat.kind, "label": beat.label,
                    "compatible_activity_ids": list(beat.compatible_activity_ids),
                    "return_to": beat.return_to,
                }
                for beat in mainline.plan.beats
            ]
            allowed_activities = [
                item["id"] for item in context.get("eligible_activities", [])
            ]
            facts = {
                "base": {
                    "stream_session_id": mainline.stream_session_id,
                    "plan_version": mainline.plan_version,
                    "beat_version": mainline.beat_version,
                    "activity_version": activity.version,
                },
                "theme": {"id": mainline.theme_id, "date": mainline.theme_date},
                "plan": {
                    "direction": mainline.plan.direction,
                    "beats": allowed_beats,
                },
                "current": {
                    "beat_id": mainline.current_beat_id,
                    "activity_id": activity.activity_id,
                    "activity_category": activity.category,
                    "remaining_seconds": context.get("remaining_seconds"),
                },
                "persona": {
                    key: round(float(context.get(key, 0.0)), 3)
                    for key in ("mood", "stress", "darkness", "fatigue", "arousal")
                },
                "room": asdict(signals),
                "allowed": {
                    "beat_ids": [item["id"] for item in allowed_beats],
                    "activity_ids": allowed_activities,
                    "template_families": sorted(TEMPLATE_FAMILIES),
                    "animation_ids": sorted(ANIMATION_IDS),
                },
            }
            messages = [
                {
                    "role": "system",
                    "content": build_system_persona_card()[:700] + "\n"
                    "你是低频直播节奏候选器，不是人格核心。绝大多数情况输出 CONTINUE。"
                    "只能从 allowed 中选择，不能输出思维链、台词正文或新事实。",
                },
                {
                    "role": "user",
                    "content": "根据以下已验证事实输出 streamer-action-decision-v1 JSON。"
                    "decision 只能为 CONTINUE/ACT；fact_mutations 最多2项，"
                    "performance_actions 最多2项。CONTINUE 时两个数组必须为空。\n"
                    + json.dumps(facts, ensure_ascii=False, separators=(",", ":")),
                },
            ]
            result = await self.service.run(
                messages=messages, role="stream_director", temperature=0.1,
                timeout=settings.ai.stream_director_timeout,
                response_format={"type": "object"},
            )
            payload = self._parse_json(result.get("reply", ""))
            decision = self._validate(payload, context)
            self.last_audit = {
                "model": result.get("model"),
                "provider": result.get("provider"),
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "input_summary": {
                    "stream_session_id": mainline.stream_session_id,
                    "theme_id": mainline.theme_id,
                    "beat_id": mainline.current_beat_id,
                    "activity_id": activity.activity_id,
                    "fact_digest": hashlib.sha256(
                        json.dumps(facts, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                },
                "validated": decision is not None,
                "decision": asdict(decision) if decision else None,
            }
            return decision
        except Exception as exc:
            self.last_audit = {
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "validated": False,
                "error_code": exc.__class__.__name__,
            }
            return None
        finally:
            await lease.release()

    async def polish_speech(
        self, *, template_text: str, emotion: str, context: dict[str, Any]
    ) -> str | None:
        if not settings.stream.director_ai_speak_polish_enabled:
            return None
        lease = await stream_director_work_gate.acquire(
            limit=1, max_waiters=0, wait_timeout=0.1
        )
        if not lease:
            return None
        try:
            from kangel.integrations.superchat.service import sc_service

            reply_gate = ai_reply_work_gate.snapshot()
            if reply_gate["active"] or reply_gate["waiting"] or sc_service.has_active_work():
                return None
            mainline = context["mainline"]
            activity = context["activity"]
            messages = [
                {
                    "role": "system",
                    "content": build_system_persona_card()[:700]
                    + "\n只润色给定的一句公开台词，不改变含义，不宣布任何未提交变化。",
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "template": template_text,
                        "emotion": emotion,
                        "committed_beat": mainline.current_beat_id,
                        "committed_activity": activity.activity_id,
                        "output": {"text": "单句，最多100字符"},
                    }, ensure_ascii=False),
                },
            ]
            result = await self.service.run(
                messages=messages, role="stream_director", temperature=0.2,
                timeout=settings.ai.stream_director_timeout,
                response_format={"type": "object"},
            )
            payload = self._parse_json(result.get("reply", ""))
            text = " ".join(str(payload.get("text", "")).split())
            if not text or len(text) > 100 or "\n" in text:
                return None
            return text
        except Exception:
            return None
        finally:
            await lease.release()

    @staticmethod
    def _parse_json(value: Any) -> dict[str, Any]:
        text = str(value or "").strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```")
            text = text.removesuffix("```").strip()
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("Director 输出必须是对象")
        return payload

    def _validate(
        self, payload: dict[str, Any], context: dict[str, Any]
    ) -> StreamerActionDecision | None:
        mainline = context["mainline"]
        activity = context["activity"]
        if set(payload) != {
            "schema_version", "decision", "reason_code", "base",
            "fact_mutations", "performance_actions",
        }:
            return None
        if payload.get("schema_version") != "streamer-action-decision-v1":
            return None
        decision = payload.get("decision")
        if decision not in {"CONTINUE", "ACT"}:
            return None
        base = payload.get("base") or {}
        expected = {
            "stream_session_id": mainline.stream_session_id,
            "plan_version": mainline.plan_version,
            "beat_version": mainline.beat_version,
            "activity_version": activity.version,
        }
        if not isinstance(base, dict) or set(base) != set(expected) or base != expected:
            return None
        reason = str(payload.get("reason_code", "NO_CHANGE"))
        if reason not in _REASONS:
            return None
        raw_facts = payload.get("fact_mutations", [])
        raw_performance = payload.get("performance_actions", [])
        if not isinstance(raw_facts, list) or not isinstance(raw_performance, list):
            return None
        if len(raw_facts) > 2 or len(raw_performance) > 2:
            return None
        if decision == "CONTINUE":
            if raw_facts or raw_performance:
                return None
            return StreamerActionDecision.continue_(mainline, activity, reason)
        beat_ids = {beat.beat_id for beat in mainline.plan.beats}
        activity_ids = {item["id"] for item in context.get("eligible_activities", [])}
        facts: list[FactMutation] = []
        seen_facts: set[str] = set()
        for item in raw_facts:
            if not isinstance(item, dict) or item.get("type") in seen_facts:
                return None
            if (
                item.get("type") == "SET_MAINLINE_BEAT"
                and set(item) == {"type", "target_beat_id"}
                and item.get("target_beat_id") in beat_ids
            ):
                facts.append(FactMutation("SET_MAINLINE_BEAT", target_beat_id=item["target_beat_id"]))
            elif (
                item.get("type") == "CHANGE_ACTIVITY"
                and set(item) == {"type", "target_activity_id"}
                and item.get("target_activity_id") in activity_ids
            ):
                facts.append(FactMutation("CHANGE_ACTIVITY", target_activity_id=item["target_activity_id"]))
            else:
                return None
            seen_facts.add(item["type"])
        performance: list[PerformanceAction] = []
        for item in raw_performance:
            if not isinstance(item, dict):
                return None
            if (
                item.get("type") == "SPEAK"
                and set(item) <= {"type", "template_family", "emotion_hint"}
                and set(item) >= {"type", "template_family"}
                and item.get("template_family") in TEMPLATE_FAMILIES
            ):
                performance.append(PerformanceAction(
                    "SPEAK", template_family=item["template_family"],
                    emotion_hint=str(item.get("emotion_hint", "思考"))[:20],
                ))
            elif (
                item.get("type") == "PLAY_ANIMATION"
                and set(item) == {"type", "animation_id"}
                and item.get("animation_id") in ANIMATION_IDS
            ):
                performance.append(PerformanceAction(
                    "PLAY_ANIMATION", animation_id=item["animation_id"]
                ))
            else:
                return None
        if not facts and not performance:
            return None
        return StreamerActionDecision(
            decision="ACT", reason_code=reason,
            stream_session_id=mainline.stream_session_id,
            plan_version=mainline.plan_version, beat_version=mainline.beat_version,
            activity_version=activity.version,
            fact_mutations=tuple(facts), performance_actions=tuple(performance),
            decision_source="ai",
        )


__all__ = ["AIStreamDirectorCandidate", "stream_director_work_gate"]
