"""Checkpointed Deep Reflection orchestration, isolated from live replies.

The caller owns the existing task lease/heartbeat and success transaction. This
module only writes internal task checkpoints; it never writes user memories.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from config import settings
from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
from kangel.integrations.ai.service import AIBackgroundBusy
from .impression_evidence import evidence_index, representative_excerpts
from .impression_models import (
    CriticResult, LetterDraft, parse_stage_json, referenced_ids, require_known_ids,
    validate_dossier, validate_reflection,
)
from .impression_prompts import STAGE_ROLES, archaeology_chunks
from .impression_metrics import current_impression_stage, impression_stage_metrics
from .impression_budget import effective_output_limit, build_budgeted_stage_messages


class ImpressionPipelineError(RuntimeError):
    """Only a fixed code leaves the pipeline, never a pydantic input repr."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ImpressionDeferred(ImpressionPipelineError):
    """Temporary lack of capacity/provider: release without consuming retry."""


class ImpressionExecutionLost(ImpressionPipelineError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeepReflectionPipeline:
    def __init__(self, database, ai_client, *, busy: Callable[[], bool] | None = None):
        self.database = database
        self.ai = ai_client
        self.busy = busy or self._live_busy

    def _live_busy(self) -> bool:
        gate = ai_reply_work_gate.snapshot()
        if gate["active"] or gate["waiting"]:
            return True
        with self.database._get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM sc_queue WHERE status IN ('pending', 'processing') LIMIT 1"
            ).fetchone() is not None

    async def generate(self, claimed: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        try:
            execution = _ReflectionExecution(self, claimed, snapshot)
            return await execution.run()
        except (ImpressionPipelineError, asyncio.CancelledError):
            raise
        except Exception:
            # JSON/schema/transport errors can carry raw private input. Never
            # propagate them into existing error_detail/logging code.
            raise ImpressionPipelineError("deep_reflection_failed") from None


class _ReflectionExecution:
    def __init__(self, pipeline: DeepReflectionPipeline, claimed, snapshot):
        self.pipeline = pipeline
        self.task = claimed
        self.snapshot = snapshot
        self.index = evidence_index(snapshot)
        self.config = snapshot["pipeline_config"]
        self.output_limit = effective_output_limit(snapshot)
        self.cached: dict[str, Any] = {}

    def args(self):
        return {"task_id": self.task["task_id"], "account_id": self.task["account_id"],
                "execution_token": self.task["execution_token"], "now": utc_now()}

    async def guard(self, role: str | None = None):
        if not await asyncio.to_thread(self.pipeline.database.viewer_impression_execution_active, **self.args()):
            raise ImpressionExecutionLost("execution_lost")
        if not settings.viewer_impression.enabled:
            raise ImpressionDeferred("feature_disabled")
        if role is not None:
            if not self.pipeline.ai.has_active_role(role):
                raise ImpressionDeferred("stage_provider_inactive")
            if await asyncio.to_thread(self.pipeline.busy):
                raise ImpressionDeferred("live_work_busy")

    def budget(self, stage):
        prefix = {"archaeology": "archaeologist", "merge": "archaeologist",
                  "synthesis": "synthesizer", "repair": "writer"}.get(stage, stage)
        return self.config[f"{prefix}_max_prompt_chars"]

    def validate(self, stage, output, allowed):
        evidence = {ref: self.index[ref] for ref in allowed}
        if stage in {"archaeology", "merge"}:
            model = validate_dossier(output, evidence, recent_delta_ids=set(
                self.snapshot.get("recent_delta_evidence_ids", [])
            ) & allowed)
        elif stage == "synthesis":
            model = validate_reflection(output, evidence)
        elif stage == "critic":
            model = CriticResult.model_validate(output)
        else:
            model = LetterDraft.model_validate(output)
            require_known_ids(model, evidence)
        normalized = model.model_dump()
        if len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))) > self.output_limit:
            raise ImpressionPipelineError("stage_output_budget_exceeded")
        return normalized

    async def stage(self, key, stage, payload, allowed):
        await self.guard()
        role = STAGE_ROLES[stage]
        if key not in self.cached:
            await self.guard(role)
        payload = {**payload, "output_max_chars": self.output_limit,
                   "max_letter_chars": self.config["max_output_chars"]}
        messages = await asyncio.to_thread(build_budgeted_stage_messages, self.snapshot, stage, payload, self.budget(stage))
        input_hash = hashlib.sha256(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if key in self.cached:
            result = self.cached[key]
            if result.get("input_hash") != input_hash:
                # Same IDs alone do not prove the same chunk/text/prompt. Never
                # reuse a result after a layout upgrade silently moves evidence
                # between chunks, or a frozen task payload has been altered.
                impression_stage_metrics.validation(stage, "rejected")
                raise ImpressionPipelineError("checkpoint_input_changed")
            self.validate(stage, result["output"], allowed)
            impression_stage_metrics.validation(stage, "cache_hit")
            return result
        await self.guard(role)
        started = time.perf_counter()
        stage_token = current_impression_stage.set(stage)
        try:
            result = await self.pipeline.ai.run(
                messages=messages, role=role, model_mode="role_hint",
                temperature=.45 if stage in {"writer", "repair", "synthesis"} else .2,
                response_format={"type": "json_object"},
                timeout=getattr(settings.ai, f"{role}_timeout"),
                background_preflight=lambda: self.guard(role),
            )
        except (ImpressionDeferred, ImpressionExecutionLost):
            raise
        except AIBackgroundBusy:
            raise ImpressionDeferred("background_http_capacity") from None
        except Exception:
            if not self.pipeline.ai.has_active_role(role):
                raise ImpressionDeferred("stage_provider_inactive") from None
            raise ImpressionPipelineError("stage_provider_failed") from None
        finally:
            current_impression_stage.reset(stage_token)
        await self.guard()
        try:
            output = self.validate(stage, parse_stage_json(result.get("reply", "")), allowed)
        except Exception:
            impression_stage_metrics.validation(stage, "rejected")
            raise
        checkpoint = {"output": output, "provider": result.get("provider"), "model": result.get("model"),
                      "input_hash": input_hash,
                      "latency_ms": int((time.perf_counter() - started) * 1000)}
        saved = await asyncio.to_thread(self.pipeline.database.save_viewer_impression_stage,
                                       **self.args(), stage_key=key, result=checkpoint)
        if not saved:
            raise ImpressionExecutionLost("checkpoint_rejected")
        self.cached[key] = checkpoint
        impression_stage_metrics.validation(stage, "accepted")
        return checkpoint

    async def run(self):
        checkpoints = await asyncio.to_thread(self.pipeline.database.load_viewer_impression_stages, **self.args())
        if checkpoints is None:
            raise ImpressionExecutionLost("execution_lost")
        self.cached = checkpoints
        chunks = await asyncio.to_thread(
            archaeology_chunks, self.snapshot, self.budget("archaeology") - 128,
            max_chunks=self.config["max_archaeology_chunks"],
        )
        dossiers = []
        for index, chunk in enumerate(chunks):
            result = await self.stage(f"archaeology:{index}", "archaeology", chunk,
                                      {row["id"] for row in chunk["evidence"]})
            dossiers.append(result["output"])
        # Deterministic binary merge tree: every leaf participates, no tail is
        # dropped. Stable coordinates allow recovery at any chunk or merge.
        level = 0
        while len(dossiers) > 1:
            merged = []
            for offset in range(0, len(dossiers), 2):
                pair = dossiers[offset:offset + 2]
                if len(pair) == 1:
                    merged.append(pair[0])
                    continue
                allowed = referenced_ids(pair)
                result = await self.stage(f"merge:{level}:{offset // 2}", "merge", {"dossiers": pair}, allowed)
                merged.append(result["output"])
            dossiers = merged
            level += 1
        if not dossiers:
            raise ImpressionPipelineError("empty_candidate_pool")
        dossier = dossiers[0]
        allowed = referenced_ids(dossier)
        excerpts = representative_excerpts(self.snapshot, dossier["conversation_texture"]["representative_evidence_ids"])
        context = {"dossier": dossier, "representative_evidence": excerpts}
        reflection = await self.stage("synthesis", "synthesis", context, allowed)
        context["reflection"] = reflection["output"]
        draft = await self.stage("writer", "writer", context, allowed)
        # Critic absence is only allowed by explicit frozen policy AND current
        # policy. An expired daily time window is a defer, never a bypass.
        critic_available = self.pipeline.ai.has_role("viewer_impression_critic")
        allow_without = self.config["allow_without_critic"] and settings.viewer_impression.allow_without_critic
        if critic_available or not allow_without:
            critique_context = {**context, "draft": draft["output"],
                                "draft_evidence": representative_excerpts(self.snapshot, draft["output"]["evidence_used"])}
            critic = await self.stage("critic", "critic", critique_context, allowed)
            if critic["output"]["verdict"] == "repair":
                if self.config["max_repair_passes"] < 1:
                    raise ImpressionPipelineError("critic_rejected")
                draft = await self.stage("repair", "repair", {
                    **critique_context, "critic": critic["output"],
                }, allowed)
        await self.guard()
        return {"reply": json.dumps(draft["output"], ensure_ascii=False),
                "provider": draft.get("provider"), "model": draft.get("model")}
