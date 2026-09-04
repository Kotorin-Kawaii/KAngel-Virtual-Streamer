"""Complete-JSON budgets for bounded stage artifacts and raw quote windows.

Only downstream representative excerpts may be windowed. Archaeology still
reads every frozen candidate (in chunks); no candidate is discarded here.
"""

import copy

from .impression_prompts import ImpressionBudgetError, build_stage_messages


_EXCERPT_FIELDS = frozenset({
    "viewer_message", "streamer_reply", "summary", "why_notable", "follow_up_hint", "emotional_mark",
})
_EXCERPT_LISTS = ("representative_evidence", "draft_evidence")


def effective_output_limit(snapshot):
    """Reserve room for two merge children and the four repair artifacts.

    This is a ceiling, not a target output length. Deriving it before archaeology
    avoids paying for valid leaf summaries that cannot fit their parent merge.
    No normalized model artifact is truncated to enforce the ceiling.
    """
    config = snapshot["pipeline_config"]
    limit = config["stage_output_chars"]
    for stage, budget_key, artifacts, quote_reserve in (
        ("merge", "archaeologist_max_prompt_chars", 2, 0),
        ("synthesis", "synthesizer_max_prompt_chars", 1, 4096),
        ("writer", "writer_max_prompt_chars", 2, 4096),
        ("critic", "critic_max_prompt_chars", 3, 4096),
        ("repair", "writer_max_prompt_chars", 4, 4096),
    ):
        fixed = sum(len(message["content"]) for message in build_stage_messages(snapshot, stage, {}, 10**9))
        limit = min(limit, (config[budget_key] - fixed - quote_reserve - 512) // artifacts)
    if limit < 512:
        raise ImpressionBudgetError("pipeline_fixed_budget_too_small")
    return limit


def _window(text, limit):
    if len(text) <= limit:
        return text
    left = limit // 2
    right = limit - left
    # Never concatenate separated passages into a fabricated continuous quote.
    return {"source_chars": len(text), "omitted_chars": len(text) - limit,
            "windows": [{"start": 0, "text": text[:left]},
                        {"start": len(text) - right, "text": text[-right:]}]}


def build_budgeted_stage_messages(snapshot, stage, payload, max_chars):
    try:
        return build_stage_messages(snapshot, stage, payload, max_chars)
    except ImpressionBudgetError:
        if stage not in {"synthesis", "writer", "critic", "repair"}:
            raise
    # Every requested ID and its scalar/time metadata remain present, including
    # all draft citations for the critic. Only raw prose may become explicitly
    # marked head/tail windows. The frozen snapshot remains unchanged.
    lengths = [len(row[field]) for key in _EXCERPT_LISTS for row in payload.get(key, [])
               for field in _EXCERPT_FIELDS if isinstance(row.get(field), str)]
    limit = max(lengths, default=0)
    while limit > 256:
        limit = max(256, limit // 2)
        candidate = copy.deepcopy(payload)
        for key in _EXCERPT_LISTS:
            for row in candidate.get(key, []):
                for field in _EXCERPT_FIELDS:
                    if isinstance(row.get(field), str):
                        row[field] = _window(row[field], limit)
        candidate["excerpt_policy"] = "windowed raw sources; omitted text is unavailable, not evidence"
        try:
            return build_stage_messages(snapshot, stage, candidate, max_chars)
        except ImpressionBudgetError:
            continue
    # This means fixed artifacts/metadata cannot fit, not that old history was
    # thrown away. Never send oversized/invalid JSON or silently omit citations.
    raise ImpressionBudgetError("stage_minimum_context_too_large")
