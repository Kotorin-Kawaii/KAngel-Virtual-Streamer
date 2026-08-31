"""Persona Evidence/Exemplar 选择与 Prompt 灰度辅助。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import threading
import time
from typing import Iterable, Mapping, Sequence

from .catalog import PersonaCatalog, PersonaEvidence, PersonaExemplar


@dataclass(frozen=True, slots=True)
class PersonaSelection:
    evidence: tuple[PersonaEvidence, ...]
    exemplars: tuple[PersonaExemplar, ...]


class PersonaPromptMetrics:
    """只记录低基数聚合数据，不保存 Prompt、QA 原文或观众文本。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._length_deltas: list[int] = []

    def record(self, name: str, count: int = 1) -> None:
        with self._lock:
            self._counts[name] += count

    def record_shadow_comparison(self, legacy_prompt: str, catalog_prompt: str) -> None:
        with self._lock:
            self._counts["shadow_comparisons"] += 1
            if sha256(legacy_prompt.encode("utf-8")).digest() == sha256(
                catalog_prompt.encode("utf-8")
            ).digest():
                self._counts["shadow_identical"] += 1
            self._length_deltas.append(len(catalog_prompt) - len(legacy_prompt))
            if len(self._length_deltas) > 256:
                del self._length_deltas[:-256]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            deltas = tuple(self._length_deltas)
            return {
                "counts": dict(sorted(self._counts.items())),
                "shadow_prompt_length_delta_avg": (
                    round(sum(deltas) / len(deltas), 2) if deltas else 0.0
                ),
                "shadow_prompt_length_delta_samples": len(deltas),
            }

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._length_deltas.clear()


persona_prompt_metrics = PersonaPromptMetrics()


def resolve_prompt_mode(
    configured_mode: str,
    rollout_percent: int,
    stable_key: str,
) -> str:
    """稳定分桶；shadow 永远仍发送 legacy，catalog 才可能切换。"""

    if configured_mode in ("legacy", "shadow"):
        return configured_mode
    if configured_mode != "catalog" or rollout_percent <= 0:
        return "legacy"
    if rollout_percent >= 100:
        return "catalog"
    digest = sha256((stable_key or "anonymous").encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100
    return "catalog" if bucket < rollout_percent else "legacy"


class PersonaEvidenceSelector:
    """把旧 QA Selector 命中的 QID 投影为类型安全的 Evidence。

    第一版故意复用旧相关性选择结果，以便把数据语义变化和检索算法变化拆开。
    """

    _SCOPE_LEVEL = {"public": 0, "familiar": 1, "trusted": 2}

    def __init__(self, catalog: PersonaCatalog, *, cooldown_seconds: float = 300.0):
        self.catalog = catalog
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._last_exemplar_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def select_from_legacy_matches(
        self,
        selected_qa: Sequence[Mapping[str, object]],
        *,
        evidence_limit: int = 3,
        exemplar_enabled: bool = False,
        exemplar_limit: int = 1,
        relationship_scope: str = "public",
        trigger_tags: Iterable[str] = (),
        now: float | None = None,
    ) -> PersonaSelection:
        q_ids = tuple(
            str(item.get("q_id", ""))
            for item in selected_qa
            if item.get("q_id")
        )
        evidence = self.catalog.evidence_for_qids(q_ids, limit=max(0, evidence_limit))
        persona_prompt_metrics.record("evidence_selected", len(evidence))
        if not exemplar_enabled or exemplar_limit <= 0:
            return PersonaSelection(evidence=evidence, exemplars=())

        requested_tags = set(trigger_tags)
        level = self._SCOPE_LEVEL.get(relationship_scope, 0)
        timestamp = time.monotonic() if now is None else now
        chosen: list[PersonaExemplar] = []
        for exemplar in self.catalog.exemplars_for_qids(q_ids):
            if exemplar.risk_level == "restricted":
                persona_prompt_metrics.record("exemplar_restricted")
                continue
            needed_level = self._SCOPE_LEVEL.get(exemplar.relationship_scope, 0)
            if needed_level > level:
                persona_prompt_metrics.record("exemplar_relationship_blocked")
                continue
            if exemplar.risk_level == "conditional":
                if not requested_tags or not requested_tags.intersection(exemplar.trigger_tags):
                    persona_prompt_metrics.record("exemplar_trigger_blocked")
                    continue
            with self._lock:
                last_at = self._last_exemplar_at.get(exemplar.cooldown_group, -1e18)
                if timestamp - last_at < self.cooldown_seconds:
                    persona_prompt_metrics.record("exemplar_cooldown_blocked")
                    continue
                self._last_exemplar_at[exemplar.cooldown_group] = timestamp
            chosen.append(exemplar)
            if len(chosen) >= exemplar_limit:
                break
        persona_prompt_metrics.record("exemplar_selected", len(chosen))
        return PersonaSelection(evidence=evidence, exemplars=tuple(chosen))

    async def select_persona_evidence(
        self,
        message: str,
        conversation_context: Mapping[str, object] | None = None,
        limit: int = 3,
        *,
        legacy_selector: object,
        persona_state: object | None = None,
    ) -> tuple[PersonaEvidence, ...]:
        """迁移期结构化入口；相关性暂时复用稳定的旧 QID Selector。"""

        select = getattr(legacy_selector, "select")
        matches = await select(
            message,
            persona_state,
            top_k=limit,
            conversation_context=dict(conversation_context or {}),
        )
        return self.select_from_legacy_matches(
            matches, evidence_limit=limit
        ).evidence

    def select_voice_exemplar(
        self,
        style_need: Iterable[str],
        relationship_scope: str = "public",
        runtime_state: Mapping[str, float] | None = None,
        limit: int = 1,
    ) -> tuple[PersonaExemplar, ...]:
        """按已确定的风格需求检索样例；运行状态只做门控，不做事实推理。"""

        if limit <= 0:
            return ()
        tags = set(style_need)
        if not tags:
            return ()
        # 高压/低落不自动放宽高风险样例，避免状态成为危险内容触发器。
        _ = runtime_state
        q_ids: list[str] = []
        for entry in self.catalog.entries:
            if entry.entry_type != "exemplar":
                continue
            if tags.intersection(entry.style_tags) or tags.intersection(entry.trigger_tags):
                q_ids.extend(entry.provenance.source_ids)
        pseudo_matches = [{"q_id": q_id} for q_id in q_ids if q_id.startswith("Q")]
        return self.select_from_legacy_matches(
            pseudo_matches,
            evidence_limit=0,
            exemplar_enabled=True,
            exemplar_limit=limit,
            relationship_scope=relationship_scope,
            trigger_tags=tags,
        ).exemplars


__all__ = [
    "PersonaEvidenceSelector",
    "PersonaPromptMetrics",
    "PersonaSelection",
    "persona_prompt_metrics",
    "resolve_prompt_mode",
]
