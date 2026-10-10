"""Shared prompt assets, not a decision core or an extra model stage."""
from __future__ import annotations

from config import settings
from .persona import build_persona_system_prompt, build_style_vector, resolve_prompt_mode
from .persona_card import build_system_persona_card


def runtime_persona_mode(stable_key: str = "") -> str:
    mode = resolve_prompt_mode(settings.persona.prompt_mode,
                               settings.persona.prompt_rollout_percent, stable_key)
    return "legacy" if mode == "shadow" else mode


def runtime_persona_system(stable_key: str = "") -> str:
    return (build_persona_system_prompt() if runtime_persona_mode(stable_key) == "catalog"
            else build_system_persona_card())


def runtime_persona_expression(stable_key: str, persona_state, internal_state) -> str:
    if runtime_persona_mode(stable_key) == "catalog":
        return build_style_vector(persona_state, internal_state).to_prompt()
    # Deliberately preserve Legacy's unstable/collapse expression design.
    from .prompts import streamer_reply_prompt_builder
    return (streamer_reply_prompt_builder._build_persona_influence_description(persona_state)
            + "\n" + streamer_reply_prompt_builder._build_internal_state_description(internal_state))


_TOPIC_SOURCES = {
    "game-retry-v2": ("Q68", "Q22"), "game-style-v2": ("Q68", "Q22"),
    "chat-choice-v2": ("Q62", "Q63"), "chat-hobby-v2": ("Q22", "Q60"),
    "chat-music-v2": ("Q27",),
}


def stage_persona_evidence(seed_id: str) -> list[dict]:
    """Bounded, deterministic canonical knowledge, never exemplar or new LLM retrieval."""
    from .prompts import persona_catalog
    return [{"entry_type": item.entry_type, "claim": item.canonical_claim[:240],
             "origin": item.origin, "source_ids": list(item.source_ids)}
            for item in persona_catalog.evidence_for_qids(_TOPIC_SOURCES.get(seed_id, ()), limit=2)
            if item.entry_type in {"preference", "stance"} and item.stability == "stable"]
