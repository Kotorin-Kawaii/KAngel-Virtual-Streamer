"""Internal stage contracts and deterministic evidence grounding for v2.

These objects are never HTTP response models or long-term memory producers.
Model text is untrusted; existence of a citation is necessary, not proof of its
meaning. The critic stage must additionally assess entailment and privacy.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .impression_evidence import parse_time


_SENSITIVE_INFERENCES = re.compile(
    r"(?:你|该观众|这个人).{0,12}(?:患有|患上|人格障碍|性取向是|宗教信仰是|政治倾向是|属于.{0,6}型人格)"
    r"|(?:你|该观众|这个人).{0,8}(?:变得更抑郁|更加依赖我|是同性恋|是双性恋)"
    r"|\byou\s+(?:are|have|seem|must be)\s+(?:(?:clearly|probably|a)\s+)?"
    r"(?:bipolar|depressed|gay|bisexual|schizophrenic|a personality disorder)\b",
    re.IGNORECASE,
)


def reject_sensitive_inferences(value: Any) -> None:
    # This is a conservative lexical guard, not an entailment classifier.
    # The separate critic still examines all draft claims against source text.
    if isinstance(value, dict):
        for key, child in value.items():
            if "evidence" not in key:
                reject_sensitive_inferences(child)
    elif isinstance(value, list):
        for child in value:
            reject_sensitive_inferences(child)
    elif isinstance(value, str) and _SENSITIVE_INFERENCES.search(value):
        raise ValueError("sensitive_inference")


def validate_final_content(content: str, evidence: dict[str, Any], max_chars: int) -> None:
    if len(content) > max_chars:
        raise ValueError("letter_too_long")
    folded = content.casefold()
    if any(term in folded for term in (
        "dossier", "archaeologist", "reflection", "synthesizer", "evidence", "pipeline_stage",
        "根据记录分析", "根据长期互动分析", "根据长期互动数据", "用户画像", "人格分类", "心理评估",
        "模型", "供应商", "记忆考古", "亲密度", "信任度", "熟悉度", "置信度",
    )) or re.search(r"\b(?:fragment|topic|episodic|nickname|relationship)\s*[:：]\s*\S+", folded):
        raise ValueError("internal_or_report_content")
    if any(ref.casefold() in folded for ref in evidence):
        raise ValueError("evidence_id_leak")
    if re.search(r"(?m)^\s*(?:#{1,6}\s|[-*]\s|\d+[.)、]\s*)", content):
        raise ValueError("report_list_content")
    reject_sensitive_inferences(content)


class StageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GroundedObservation(StageModel):
    summary: str = Field(min_length=1, max_length=1600)
    evidence_ids: list[str] = Field(min_length=1, max_length=24)


class TimelineObservation(GroundedObservation):
    period: str = Field(min_length=1, max_length=160)


class RecurringTheme(GroundedObservation):
    theme: str = Field(min_length=1, max_length=200)
    first_seen_at: str
    last_seen_at: str
    strength: Literal["low", "medium", "high"]


class MemorableMoment(GroundedObservation):
    why_it_matters: str = Field(min_length=1, max_length=800)


class ObservableChange(GroundedObservation):
    earlier: str = Field(min_length=1, max_length=800)
    later: str = Field(min_length=1, max_length=800)
    earlier_evidence_ids: list[str] = Field(min_length=1, max_length=12)
    later_evidence_ids: list[str] = Field(min_length=1, max_length=12)


class OpenThread(GroundedObservation):
    follow_up_hint: str = Field(min_length=1, max_length=800)


class ConversationTexture(StageModel):
    summary: str = Field(default="", max_length=1200)
    representative_evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class ViewerDossier(StageModel):
    relationship_timeline: list[TimelineObservation] = Field(default_factory=list, max_length=24)
    recurring_themes: list[RecurringTheme] = Field(default_factory=list, max_length=24)
    memorable_moments: list[MemorableMoment] = Field(default_factory=list, max_length=24)
    interaction_patterns: list[GroundedObservation] = Field(default_factory=list, max_length=24)
    changes_over_time: list[ObservableChange] = Field(default_factory=list, max_length=24)
    recent_delta: list[GroundedObservation] = Field(default_factory=list, max_length=24)
    open_threads: list[OpenThread] = Field(default_factory=list, max_length=24)
    conversation_texture: ConversationTexture = Field(default_factory=ConversationTexture)
    uncertainties: list[GroundedObservation] = Field(default_factory=list, max_length=12)


class ViewerReflection(StageModel):
    # Even subjective impressions bind to evidence; free prose cannot create
    # a second, uncited route into the writer's view of this account.
    core_impression: GroundedObservation
    what_stands_out_to_me: list[GroundedObservation] = Field(default_factory=list, max_length=12)
    how_my_view_changed: GroundedObservation | None = None
    things_i_find_endearing: list[GroundedObservation] = Field(default_factory=list, max_length=12)
    things_i_find_funny_or_annoying: list[GroundedObservation] = Field(default_factory=list, max_length=12)
    things_i_still_wonder_about: list[GroundedObservation] = Field(default_factory=list, max_length=12)
    memories_i_might_naturally_mention: list[GroundedObservation] = Field(default_factory=list, max_length=12)
    recent_feeling: GroundedObservation | None = None
    overall_tone: Literal["warm", "playful", "sincere", "reflective"]


class LetterDraft(StageModel):
    content: str = Field(min_length=1, max_length=10000)
    tone: Literal["warm", "playful", "sincere", "reflective"]
    evidence_used: list[str] = Field(min_length=1, max_length=24)


class CriticResult(StageModel):
    verdict: Literal["pass", "repair"]
    unsupported_claims: list[str] = Field(default_factory=list, max_length=24)
    overinterpretations: list[str] = Field(default_factory=list, max_length=24)
    timeline_errors: list[str] = Field(default_factory=list, max_length=24)
    privacy_leaks: list[str] = Field(default_factory=list, max_length=24)
    sensitive_inferences: list[str] = Field(default_factory=list, max_length=24)
    report_tone: list[str] = Field(default_factory=list, max_length=24)
    repair_instructions: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def consistent_verdict(self):
        issues = any(getattr(self, field) for field in (
            "unsupported_claims", "overinterpretations", "timeline_errors", "privacy_leaks",
            "sensitive_inferences", "report_tone", "repair_instructions",
        ))
        if self.verdict == "pass" and issues:
            raise ValueError("critic_pass_with_issues")
        if self.verdict == "repair" and not self.repair_instructions:
            raise ValueError("critic_repair_without_instructions")
        return self


def parse_stage_json(raw: str) -> dict[str, Any]:
    # Do not echo validation exceptions into ordinary logs: pydantic includes
    # the offending input, which can be private evidence or a draft.
    if len(raw) > 500000:
        raise ValueError("stage_output_too_large")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_stage_key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non_finite_stage_number")

    value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("stage_output_not_object")
    return value


def referenced_ids(value: Any) -> set[str]:
    if isinstance(value, BaseModel):
        value = value.model_dump()
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"evidence_ids", "earlier_evidence_ids", "later_evidence_ids",
                       "representative_evidence_ids", "evidence_used"}:
                result.update(child)
            else:
                result.update(referenced_ids(child))
    elif isinstance(value, list):
        for child in value:
            result.update(referenced_ids(child))
    return result


def require_known_ids(value: StageModel, evidence: dict[str, dict[str, Any]]) -> None:
    if referenced_ids(value) - evidence.keys():
        raise ValueError("unknown_evidence_id")


def _evidence_interval(evidence: dict[str, Any]):
    # Topic/relationship aggregates span time. Treating last_seen_at as a point
    # could incorrectly certify an overlapping aggregate as an "after" event.
    for key in ("created_at", "occurred_at"):
        if evidence.get(key):
            instant = parse_time(evidence[key])
            return instant, instant
    for start_key, end_key in (("first_seen_at", "last_seen_at"), ("started_at", "ended_at")):
        if evidence.get(start_key):
            start = parse_time(evidence[start_key])
            end = parse_time(evidence[end_key]) if evidence.get(end_key) else start
            if end < start:
                raise ValueError("evidence_timeline_invalid")
            return start, end
    if evidence.get("last_seen_at"):
        instant = parse_time(evidence["last_seen_at"])
        return instant, instant
    raise ValueError("missing_evidence_time")


def validate_dossier(raw: dict[str, Any], evidence: dict[str, dict[str, Any]], *,
                     recent_delta_ids: set[str] | None = None) -> ViewerDossier:
    cleaned = copy.deepcopy(raw)
    for field in ("relationship_timeline", "recurring_themes", "memorable_moments",
                  "interaction_patterns", "changes_over_time", "recent_delta", "open_threads", "uncertainties"):
        if isinstance(cleaned.get(field), list):
            # Uncited observations disappear rather than becoming free-form
            # factual prose in a later stage. Malformed/forged IDs still fail.
            cleaned[field] = [item for item in cleaned[field]
                              if not isinstance(item, dict) or item.get("evidence_ids")]
    dossier = ViewerDossier.model_validate(cleaned)
    require_known_ids(dossier, evidence)
    reject_sensitive_inferences(dossier.model_dump())
    texture = dossier.conversation_texture
    if texture.summary and not texture.representative_evidence_ids:
        dossier.conversation_texture = ConversationTexture()
    if any(not ref.startswith("fragment:") for ref in texture.representative_evidence_ids):
        raise ValueError("texture_requires_conversation")
    for change in dossier.changes_over_time:
        before, after = set(change.earlier_evidence_ids), set(change.later_evidence_ids)
        if before & after or before | after != set(change.evidence_ids):
            raise ValueError("change_requires_distinct_before_after")
        if max(_evidence_interval(evidence[ref])[1] for ref in before) >= min(
            _evidence_interval(evidence[ref])[0] for ref in after
        ):
            raise ValueError("change_timeline_invalid")
    for theme in dossier.recurring_themes:
        first, last = parse_time(theme.first_seen_at), parse_time(theme.last_seen_at)
        intervals = [_evidence_interval(evidence[ref]) for ref in theme.evidence_ids]
        if first > last or first < min(start for start, _ in intervals) or last > max(end for _, end in intervals):
            raise ValueError("theme_timeline_unsupported")
    if recent_delta_ids is not None:
        for observation in dossier.recent_delta:
            # Historical citations may support a comparison, but at least one
            # genuinely newer source must support a claim of new information.
            if not set(observation.evidence_ids) & recent_delta_ids:
                raise ValueError("recent_delta_requires_new_evidence")
    # Repeated patterns cannot be manufactured from a one-off fragment. An
    # existing topic summary may itself carry a verified multiple-source count.
    recurring_claims = [*dossier.interaction_patterns,
                        *(theme for theme in dossier.recurring_themes if theme.strength != "low")]
    for pattern in recurring_claims:
        if len(set(pattern.evidence_ids)) < 2 and not any(
            ref.startswith("topic:") and int(evidence[ref].get("source_count") or 0) >= 2
            for ref in pattern.evidence_ids
        ):
            raise ValueError("pattern_requires_repeated_evidence")
    if not referenced_ids(dossier):
        raise ValueError("empty_grounded_dossier")
    return dossier


def validate_reflection(raw: dict[str, Any], evidence: dict[str, dict[str, Any]]) -> ViewerReflection:
    reflection = ViewerReflection.model_validate(raw)
    require_known_ids(reflection, evidence)
    reject_sensitive_inferences(reflection.model_dump())
    return reflection
