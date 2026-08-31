"""版本化 Persona Catalog。

Catalog 以现有官方 101 问为只读来源，并把“知识”和“表演样例”拆成不同
的数据类型。这里刻意不依赖 Prompt Builder，避免结构化资产反向绑死运行时。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import json
from typing import Any, Iterable, Literal, Mapping, Sequence


EntryType = Literal["fact", "preference", "stance", "exemplar"]
Stability = Literal["stable", "historical", "time_bound", "performative"]
RiskLevel = Literal["normal", "conditional", "restricted"]


class PersonaCatalogError(ValueError):
    """Catalog 数据不满足静态契约。"""


@dataclass(frozen=True, slots=True)
class PersonaProvenance:
    origin: Literal["official", "project_original"]
    source_kind: str
    source_uri: str
    source_locale: str
    source_ids: tuple[str, ...]
    evidence_relation: Literal["verbatim", "normalized", "adapted"]


@dataclass(frozen=True, slots=True)
class PersonaCatalogEntry:
    id: str
    entry_type: EntryType
    topics: tuple[str, ...]
    scope: str
    stability: Stability
    provenance: PersonaProvenance
    source_question: str
    source_answer: str
    canonical_claim: str = ""
    example_text: str = ""
    style_tags: tuple[str, ...] = ()
    trigger_tags: tuple[str, ...] = ()
    risk_level: RiskLevel = "normal"
    relationship_scope: str = "public"
    cooldown_group: str = ""
    do_not_copy: bool = False
    retrieval_enabled: bool = True
    retrieval_weight: float = 1.0

    def __post_init__(self) -> None:
        if self.entry_type == "exemplar":
            if not self.example_text or self.canonical_claim:
                raise PersonaCatalogError(
                    f"exemplar {self.id} 必须只有 example_text，不能有 canonical_claim"
                )
            if not self.do_not_copy:
                raise PersonaCatalogError(f"exemplar {self.id} 必须标记 do_not_copy")
        elif not self.canonical_claim or self.example_text:
            raise PersonaCatalogError(
                f"knowledge {self.id} 必须只有 canonical_claim，不能有 example_text"
            )


@dataclass(frozen=True, slots=True)
class PersonaEvidence:
    id: str
    entry_type: Literal["fact", "preference", "stance"]
    canonical_claim: str
    topics: tuple[str, ...]
    stability: Stability
    source_ids: tuple[str, ...]
    origin: Literal["official", "project_original"]
    weight: float


@dataclass(frozen=True, slots=True)
class PersonaExemplar:
    id: str
    example_text: str
    style_tags: tuple[str, ...]
    trigger_tags: tuple[str, ...]
    risk_level: RiskLevel
    relationship_scope: str
    cooldown_group: str
    source_ids: tuple[str, ...]
    do_not_copy: Literal[True] = True


@dataclass(frozen=True, slots=True)
class PersonaCatalog:
    catalog_id: str
    schema_version: int
    source_hash: str
    source_uri: str
    reference_uri: str
    entries: tuple[PersonaCatalogEntry, ...]
    legacy_qa: tuple[Mapping[str, str], ...]

    def entries_for_source_id(self, q_id: str) -> tuple[PersonaCatalogEntry, ...]:
        return tuple(
            entry for entry in self.entries if q_id in entry.provenance.source_ids
        )

    def evidence_for_qids(
        self, q_ids: Iterable[str], *, limit: int = 3
    ) -> tuple[PersonaEvidence, ...]:
        result: list[PersonaEvidence] = []
        seen: set[str] = set()
        for q_id in q_ids:
            for entry in self.entries_for_source_id(q_id):
                if entry.entry_type == "exemplar" or not entry.retrieval_enabled:
                    continue
                if entry.id in seen:
                    continue
                seen.add(entry.id)
                result.append(
                    PersonaEvidence(
                        id=entry.id,
                        entry_type=entry.entry_type,
                        canonical_claim=entry.canonical_claim,
                        topics=entry.topics,
                        stability=entry.stability,
                        source_ids=entry.provenance.source_ids,
                        origin=entry.provenance.origin,
                        weight=entry.retrieval_weight,
                    )
                )
                if len(result) >= limit:
                    return tuple(result)
        return tuple(result)

    def exemplars_for_qids(self, q_ids: Iterable[str]) -> tuple[PersonaExemplar, ...]:
        result: list[PersonaExemplar] = []
        seen: set[str] = set()
        for q_id in q_ids:
            for entry in self.entries_for_source_id(q_id):
                if entry.entry_type != "exemplar" or not entry.retrieval_enabled:
                    continue
                if entry.id in seen:
                    continue
                seen.add(entry.id)
                result.append(
                    PersonaExemplar(
                        id=entry.id,
                        example_text=entry.example_text,
                        style_tags=entry.style_tags,
                        trigger_tags=entry.trigger_tags,
                        risk_level=entry.risk_level,
                        relationship_scope=entry.relationship_scope,
                        cooldown_group=entry.cooldown_group,
                        source_ids=entry.provenance.source_ids,
                    )
                )
        return tuple(result)

    def project_legacy_qa(self, q_ids: Iterable[str] | None = None) -> list[dict[str, str]]:
        """兼容旧 selector 的无损投影，不让 Catalog 改变旧 Prompt。"""

        allowed = set(q_ids) if q_ids is not None else None
        return [
            dict(item)
            for item in self.legacy_qa
            if allowed is None or item["q_id"] in allowed
        ]


_ENTRY_TYPES: tuple[EntryType, ...] = ("fact", "preference", "stance", "exemplar")
_STABILITY_ORDER: tuple[Stability, ...] = (
    "historical",
    "time_bound",
    "performative",
    "stable",
)
_TOPIC_OVERRIDES: dict[str, tuple[str, ...]] = {
    "Q01": ("identity", "internet_angel"),
    "Q03": ("identity", "birthday"),
    "Q04": ("identity", "age"),
    "Q09": ("identity", "occupation"),
    "Q14": ("commerce", "brand_collaboration"),
    "Q19": ("fashion", "brand_preference"),
    "Q40": ("internet", "recognition"),
    "Q49": ("viewer_interaction", "performative_flirting"),
    "Q55": ("distress", "high_risk_language"),
    "Q82": ("brand", "historical_collaboration"),
    "Q85": ("health", "historical_experience"),
    "Q88": ("relationship", "performative_stance"),
    "Q90": ("viewer_interaction", "performative_flirting"),
    "Q92": ("high_risk_language", "offline_evaluation"),
    "Q93": ("high_risk_language", "offline_evaluation"),
}


def _load_manifest() -> dict[str, Any]:
    manifest_path = files(__package__).joinpath("catalog_manifest.json")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _load_official_snapshot() -> tuple[Mapping[str, Any], list[Mapping[str, str]]]:
    snapshot_path = files(__package__).joinpath("official_qa_snapshot.json")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if snapshot.get("schema_version") != 1 or snapshot.get("origin") != "official":
        raise PersonaCatalogError("official_qa_snapshot.json 来源标记非法")
    items = snapshot.get("items")
    if not isinstance(items, list):
        raise PersonaCatalogError("official_qa_snapshot.json 缺少 items")
    return snapshot, items


def calculate_source_hash(qa_items: Sequence[Mapping[str, str]]) -> str:
    normalized = [
        {
            "q_id": item["q_id"],
            "question": item["question"],
            "answer": item["answer"],
        }
        for item in qa_items
    ]
    payload = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _qid_number(q_id: str) -> int:
    if (
        not q_id.startswith("Q")
        or len(q_id) not in (3, 4)
        or not q_id[1:].isdigit()
    ):
        raise PersonaCatalogError(f"非法 QID: {q_id}")
    number = int(q_id[1:])
    if number < 1 or number > 101:
        raise PersonaCatalogError(f"QID 超出范围: {q_id}")
    return number


def _validate_source(qa_items: Sequence[Mapping[str, str]]) -> tuple[dict[str, Mapping[str, str]], str]:
    by_id: dict[str, Mapping[str, str]] = {}
    for item in qa_items:
        q_id = item.get("q_id", "")
        _qid_number(q_id)
        if q_id in by_id:
            raise PersonaCatalogError(f"重复 QID: {q_id}")
        if not item.get("question") or not item.get("answer"):
            raise PersonaCatalogError(f"{q_id} 缺少官方问题或答案")
        by_id[q_id] = item
    expected = {f"Q{number:02d}" for number in range(1, 102)}
    if set(by_id) != expected:
        missing = sorted(expected - set(by_id))
        extra = sorted(set(by_id) - expected)
        raise PersonaCatalogError(f"101 问覆盖异常 missing={missing} extra={extra}")
    ordered = [by_id[f"Q{number:02d}"] for number in range(1, 102)]
    return by_id, calculate_source_hash(ordered)


def _classification(manifest: Mapping[str, Any]) -> dict[str, EntryType]:
    result: dict[str, EntryType] = {}
    raw = manifest["primary_classification"]
    if set(raw) != set(_ENTRY_TYPES):
        raise PersonaCatalogError("primary_classification 类型集合不完整")
    for entry_type in _ENTRY_TYPES:
        for q_id in raw[entry_type]:
            _qid_number(q_id)
            if q_id in result:
                raise PersonaCatalogError(f"{q_id} 被重复主分类")
            result[q_id] = entry_type
    expected = {f"Q{number:02d}" for number in range(1, 102)}
    if set(result) != expected:
        raise PersonaCatalogError("primary_classification 未完整覆盖 Q01-Q101")
    return result


def _stability(q_id: str, manifest: Mapping[str, Any]) -> Stability:
    overrides = manifest.get("stability_overrides", {})
    for stability in _STABILITY_ORDER[:-1]:
        if q_id in overrides.get(stability, ()):
            return stability
    return "stable"


def _default_claim(entry_type: EntryType, question: str, answer: str) -> str:
    if entry_type == "fact":
        return f"关于“{question}”的官方设定是：{answer}"
    if entry_type == "preference":
        return f"关于“{question}”，她公开表达的偏好是：{answer}"
    return f"面对“{question}”时，她通常采用这样的公开姿态：{answer}"


def _provenance(
    source: Mapping[str, Any], source_ids: Iterable[str], *, relation: str = "normalized"
) -> PersonaProvenance:
    return PersonaProvenance(
        origin="official",
        source_kind=source["source_kind"],
        source_uri=source["source_uri"],
        source_locale=source["source_locale"],
        source_ids=tuple(source_ids),
        evidence_relation=relation,  # type: ignore[arg-type]
    )


def build_persona_catalog(
    qa_items: Sequence[Mapping[str, str]],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> PersonaCatalog:
    """从官方 QA 快照构建严格、只读的运行时 Catalog。"""

    data = dict(manifest or _load_manifest())
    if data.get("schema_version") != 1:
        raise PersonaCatalogError("不支持的 Catalog schema_version")
    source = data["source"]
    if source.get("origin") != "official":
        raise PersonaCatalogError("官方 101 问来源必须标记 origin=official")

    by_id, source_hash = _validate_source(qa_items)
    expected_hash = source.get("expected_source_hash")
    if expected_hash not in (None, "", "TO_BE_FROZEN") and expected_hash != source_hash:
        raise PersonaCatalogError(
            f"官方 QA 快照 hash 不匹配 expected={expected_hash} actual={source_hash}"
        )
    classification = _classification(data)
    canonical_overrides = data.get("canonical_overrides", {})
    exemplar_policies = data.get("exemplar_policies", {})

    clustered_qids: set[str] = set()
    cluster_entries: list[PersonaCatalogEntry] = []
    for cluster in data.get("knowledge_clusters", ()):
        source_ids = tuple(cluster["source_ids"])
        if not source_ids or any(classification[q_id] == "exemplar" for q_id in source_ids):
            raise PersonaCatalogError(f"知识簇 {cluster['id']} 引用了 exemplar-only QID")
        if clustered_qids.intersection(source_ids):
            raise PersonaCatalogError(f"知识簇 {cluster['id']} 与其他知识簇重复")
        clustered_qids.update(source_ids)
        cluster_entries.append(
            PersonaCatalogEntry(
                id=cluster["id"],
                entry_type=cluster["entry_type"],
                canonical_claim=cluster["canonical_claim"],
                topics=tuple(cluster.get("topics", ("official_qa",))),
                scope="public_persona",
                stability=cluster.get("stability", "stable"),
                provenance=_provenance(source, source_ids),
                source_question=" / ".join(by_id[q_id]["question"] for q_id in source_ids),
                source_answer=" / ".join(by_id[q_id]["answer"] for q_id in source_ids),
            )
        )

    entries: list[PersonaCatalogEntry] = []
    for number in range(1, 102):
        q_id = f"Q{number:02d}"
        item = by_id[q_id]
        entry_type = classification[q_id]
        policy = exemplar_policies.get(q_id, {})
        if entry_type != "exemplar" and q_id not in clustered_qids:
            claim = canonical_overrides.get(q_id) or _default_claim(
                entry_type, item["question"], item["answer"]
            )
            entries.append(
                PersonaCatalogEntry(
                    id=f"persona.{q_id.lower()}.{entry_type}",
                    entry_type=entry_type,
                    canonical_claim=claim,
                    topics=_TOPIC_OVERRIDES.get(q_id, ("official_qa", q_id.lower())),
                    scope="public_persona",
                    stability=_stability(q_id, data),
                    provenance=_provenance(source, (q_id,)),
                    source_question=item["question"],
                    source_answer=item["answer"],
                )
            )

        # exemplar-only QA 以及显式配置的附属 exemplar 都使用独立载荷。
        if entry_type == "exemplar" or policy:
            entries.append(
                PersonaCatalogEntry(
                    id=f"persona.{q_id.lower()}.exemplar",
                    entry_type="exemplar",
                    example_text=item["answer"],
                    topics=_TOPIC_OVERRIDES.get(q_id, ("official_qa", q_id.lower())),
                    scope="public_persona",
                    stability=_stability(q_id, data),
                    provenance=_provenance(source, (q_id,), relation="verbatim"),
                    source_question=item["question"],
                    source_answer=item["answer"],
                    style_tags=tuple(policy.get("style_tags", ("official_voice",))),
                    trigger_tags=tuple(policy.get("trigger_tags", ())),
                    risk_level=policy.get("risk_level", "normal"),
                    relationship_scope=policy.get("relationship_scope", "public"),
                    cooldown_group=policy.get("cooldown_group", "persona_exemplar"),
                    do_not_copy=True,
                    retrieval_enabled=policy.get("risk_level", "normal") != "restricted",
                    retrieval_weight=float(policy.get("weight", 1.0)),
                )
            )

    entries.extend(cluster_entries)
    for policy in data.get("project_original_entries", ()):
        entries.append(
            PersonaCatalogEntry(
                id=policy["id"],
                entry_type=policy["entry_type"],
                canonical_claim=policy["canonical_claim"],
                topics=tuple(policy.get("topics", ("runtime_policy",))),
                scope=policy.get("scope", "runtime_policy"),
                stability=policy.get("stability", "stable"),
                provenance=PersonaProvenance(
                    origin="project_original",
                    source_kind="project_persona_policy",
                    source_uri="repository://persona/catalog_manifest.json",
                    source_locale="zh-CN",
                    source_ids=tuple(policy.get("source_refs", ())),
                    evidence_relation="adapted",
                ),
                source_question="",
                source_answer="",
                retrieval_enabled=bool(policy.get("retrieval_enabled", False)),
            )
        )

    def entry_sort_key(entry: PersonaCatalogEntry) -> tuple[int, str]:
        source_id = entry.provenance.source_ids[0] if entry.provenance.source_ids else ""
        return (
            _qid_number(source_id) if source_id.startswith("Q") else 1000,
            entry.id,
        )

    entries.sort(key=entry_sort_key)
    legacy = tuple(
        {
            "q_id": f"Q{number:02d}",
            "question": by_id[f"Q{number:02d}"]["question"],
            "answer": by_id[f"Q{number:02d}"]["answer"],
        }
        for number in range(1, 102)
    )
    return PersonaCatalog(
        catalog_id=data["catalog_id"],
        schema_version=data["schema_version"],
        source_hash=source_hash,
        source_uri=source["source_uri"],
        reference_uri=source["reference_uri"],
        entries=tuple(entries),
        legacy_qa=legacy,
    )


def load_persona_catalog() -> PersonaCatalog:
    """从版本化静态快照加载 Catalog；默认运行和测试均不访问网络。"""

    manifest = _load_manifest()
    snapshot, items = _load_official_snapshot()
    catalog = build_persona_catalog(items, manifest=manifest)
    if snapshot.get("source_hash") != catalog.source_hash:
        raise PersonaCatalogError("静态快照自身 hash 与内容不一致")
    if snapshot.get("source_uri") != catalog.source_uri:
        raise PersonaCatalogError("静态快照 source_uri 与 manifest 不一致")
    return catalog


__all__ = [
    "PersonaCatalog",
    "PersonaCatalogEntry",
    "PersonaCatalogError",
    "PersonaEvidence",
    "PersonaExemplar",
    "PersonaProvenance",
    "build_persona_catalog",
    "calculate_source_hash",
    "load_persona_catalog",
]
