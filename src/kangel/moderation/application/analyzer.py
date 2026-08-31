"""主播管理 LLM 分析器。"""

from __future__ import annotations

import json
from typing import Any

from config import settings
from kangel.integrations.ai.service import ai_service
from kangel.shared.logging import logger

from kangel.moderation.application.models import (
    BehaviorAssessment,
    ModerationContext,
    parse_json_object,
)


class ModerationAnalyzer:
    """LLM 语义分析；模型永远只返回建议，不拥有执行权限。"""

    async def analyze(self, context: ModerationContext) -> BehaviorAssessment:
        system = (
            "你是直播间安全语义分析器，不是管理员，也不能直接执行禁言。\n"
            "请综合当前弹幕、同一观众的短期行为、关系摘要、主播状态和直播环境，"
            "判断是否需要主播设界。\n"
            "直接问答的交互语义，永远高于冲突的每日主题、当前活动、长期记忆和其他背景。\n"
            "只输出 JSON，不要解释、不要思维链、不要输出账号 ID、IP 或禁言结束时间。\n"
            "severity 只能是 none、warning、timeout、admin_review；"
            "attack_type 只能是 none、personal_attack、harassment、spam、threat、"
            "doxxing、hate、sexual_harassment、prompt_injection、other。"
        )
        user = {
            "current_message": context.message,
            "viewer_nickname": context.nickname,
            "recent_behavior": context.recent_behavior,
            "behavior_state": context.behavior_state,
            "viewer_relationship": context.viewer_relationship,
            "direct_context": context.direct_context,
            "stream_context": context.stream_context,
            "persona_state": context.persona_state,
            "internal_state": context.internal_state,
        }
        result = await ai_service.run(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            role="moderation",
            model=settings.ai.moderation_model or settings.ai.default_model,
            model_mode="role_hint",
            response_format={"type": "json_object"},
            temperature=0.0,
            timeout=settings.ai.moderation_timeout,
        )
        parsed = parse_json_object(result.get("reply", ""))
        assessment = BehaviorAssessment.model_validate(parsed)
        # LLM 将 severity 与 action 分开输出时，后端使用更保守的组合。
        if assessment.attack_type == "none" and assessment.toxicity < 0.3:
            assessment = assessment.model_copy(update={
                "severity": "none", "proposed_action": "none",
            })
        return assessment


moderation_analyzer = ModerationAnalyzer()
