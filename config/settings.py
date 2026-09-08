from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
import re


class ServerConfig(BaseSettings):
    """服务器配置"""
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    log_level: str = "debug"


class ProviderModelMapping(BaseModel):
    """供应商支持的各角色模型名；未配的普通角色回退到 default。

    ``viewer_impression`` 是隐私敏感的独立旁路，必须显式配置，不参与该回退。
    """
    default: str = ""
    qa_selector: Optional[str] = None
    danmaku_selector: Optional[str] = None
    impact_analysis: Optional[str] = None
    intent_shadow: Optional[str] = None
    moderation: Optional[str] = None
    session_memory: Optional[str] = None
    stream_director: Optional[str] = None
    viewer_impression: Optional[str] = None
    viewer_memory_archaeologist: Optional[str] = None
    viewer_impression_synthesizer: Optional[str] = None
    viewer_impression_critic: Optional[str] = None


class ProviderReasoningMapping(BaseModel):
    """按角色声明 reasoning 强度；只有供应商显式支持时才会发出。"""
    default: Optional[Literal["off", "low", "medium", "high"]] = None
    qa_selector: Optional[Literal["off", "low", "medium", "high"]] = None
    danmaku_selector: Optional[Literal["off", "low", "medium", "high"]] = None
    impact_analysis: Optional[Literal["off", "low", "medium", "high"]] = "low"
    intent_shadow: Optional[Literal["off", "low", "medium", "high"]] = None
    moderation: Optional[Literal["off", "low", "medium", "high"]] = None
    session_memory: Optional[Literal["off", "low", "medium", "high"]] = None
    stream_director: Optional[Literal["off", "low", "medium", "high"]] = None
    viewer_impression: Optional[Literal["off", "low", "medium", "high"]] = None
    viewer_memory_archaeologist: Optional[Literal["off", "low", "medium", "high"]] = None
    viewer_impression_synthesizer: Optional[Literal["off", "low", "medium", "high"]] = None
    viewer_impression_critic: Optional[Literal["off", "low", "medium", "high"]] = None


class AIProvider(BaseModel):
    """单个模型供应商配置。

    weight 决定同时间段内优先级（降序），请求失败自动回退到下一供应商。
    active_start/active_end 为 HH:MM 本地时间，支持跨午夜（如 22:00-06:00）。
    """
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    models: ProviderModelMapping = Field(default_factory=ProviderModelMapping)
    reasoning_protocol: Literal["none", "openai"] = "none"
    reasoning: ProviderReasoningMapping = Field(default_factory=ProviderReasoningMapping)
    enabled: bool = True
    weight: int = Field(default=100, ge=0)
    active_start: str = "00:00"
    active_end: str = "23:59"

    @field_validator("active_start", "active_end")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        parts = v.strip().split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"时间格式必须为 HH:MM，得到: {v}")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"时间超出范围: {v}")
        return f"{h:02d}:{m:02d}"


class AIModelPrice(BaseModel):
    """每 100 万 token 的单价；只用于读取时折算花费，不参与调用决策。

    model 为精确模型名，或 "*" 作为未列出模型的兜底。cached_input_per_1m 留空时
    缓存命中的输入 token 按普通输入价计算。
    """
    model: str = ""
    input_per_1m: float = Field(default=0.0, ge=0)
    output_per_1m: float = Field(default=0.0, ge=0)
    cached_input_per_1m: Optional[float] = Field(default=None, ge=0)
    currency: str = "CNY"

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("价目表条目必须提供 model（精确模型名或 * 兜底）")
        return value

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        value = v.strip().upper()
        if not value:
            raise ValueError("价目表条目的 currency 不能为空")
        return value


class AIConfig(BaseSettings):
    """OpenAI-compatible AI 服务配置。

    向后兼容：providers 为空时使用 base_url/api_key/default_model 单供应商模式。
    配置 providers 后，各调用点通过 role 而非 model 选择供应商链。
    """
    # —— 单供应商兼容字段（providers 为空时生效）——
    base_url: str = Field(default="https://api.siliconflow.cn/v1", description="OpenAI-compatible API 基础URL")
    api_key: str = Field(default="", description="API密钥")
    default_model: str = Field(default="Pro/MiniMaxAI/MiniMax-M2.5", description="默认模型")
    qa_selector_model: Optional[str] = Field(default=None, description="QA选择器模型，留空时使用默认模型")
    qa_selector_timeout: int = Field(default=20, description="QA选择器API超时时间(秒)")
    danmaku_selector_model: Optional[str] = Field(default=None, description="弹幕候选选择模型，留空时使用默认模型")
    danmaku_selector_timeout: int = Field(default=15, description="弹幕候选选择超时时间(秒)")
    impact_analysis_model: Optional[str] = Field(default=None, description="人格影响分析模型，留空时使用默认模型")
    impact_analysis_timeout: int = Field(default=30, description="人格影响分析超时时间(秒)")
    intent_shadow_model: Optional[str] = Field(default=None, description="意图候选轻量模型，留空时不额外调用模型")
    moderation_model: Optional[str] = Field(default=None, description="主播管理语义分析模型，留空时使用默认模型")
    moderation_timeout: int = Field(default=20, ge=1, le=120, description="主播管理语义分析超时时间(秒)")
    session_memory_model: Optional[str] = Field(default=None, description="下播情景记忆总结模型，留空时使用默认模型")
    session_memory_timeout: int = Field(default=60, ge=5, le=300, description="下播情景记忆总结超时时间(秒)")
    stream_director_model: Optional[str] = Field(default=None, description="可选直播导演模型，留空时使用默认模型")
    stream_director_timeout: int = Field(default=12, ge=3, le=60)
    viewer_impression_model: Optional[str] = Field(
        default=None, description="Viewer Impression 专用模型；留空表示该功能不可用"
    )
    viewer_impression_timeout: int = Field(default=300, ge=5, le=900)
    viewer_memory_archaeologist_model: Optional[str] = None
    viewer_impression_synthesizer_model: Optional[str] = None
    viewer_impression_critic_model: Optional[str] = None
    viewer_memory_archaeologist_timeout: int = Field(default=600, ge=5, le=1800)
    viewer_impression_synthesizer_timeout: int = Field(default=300, ge=5, le=900)
    viewer_impression_critic_timeout: int = Field(default=300, ge=5, le=900)

    # —— 多供应商配置 ——
    providers: list[AIProvider] = Field(
        default_factory=list,
        description="供应商列表；为空时回退到单供应商兼容模式",
    )

    # —— 功能开关 ——
    event_appraisal_enabled: bool = Field(
        default=True,
        description="启用受限事件评价投影；关闭后兼容旧三轴影响结构",
    )
    parallel_context_analysis: bool = Field(
        default=True,
        description="并行执行QA选择与人格影响分析",
    )
    intent_shadow_enabled: bool = Field(default=False, description="影子模式运行非阻塞意图候选")
    intent_candidate_apply_enabled: bool = Field(default=False, description="允许复用已完成分析受控调整当轮计划")
    intent_shadow_timeout: int = Field(default=4, ge=1, le=30)
    intent_shadow_concurrency: int = Field(default=1, ge=1, le=4)
    intent_shadow_max_waiters: int = Field(default=2, ge=0, le=20)
    intent_shadow_max_per_stream: int = Field(default=12, ge=0, le=1000)
    temperature: float = Field(default=0.2, description="温度参数")
    streaming: bool = Field(default=False, description="是否启用流式输出")
    timeout: int = Field(default=600, description="请求超时时间(秒)")

    # —— Token 花费折算价目表（只读取时使用）——
    pricing: list[AIModelPrice] = Field(
        default_factory=list,
        description="模型单价列表；为空时后台只显示 token 不折算金额",
    )

    @model_validator(mode="after")
    def validate_pricing(self):
        seen: set[str] = set()
        currencies: set[str] = set()
        for item in self.pricing:
            key = item.model.casefold()
            if key in seen:
                raise ValueError(f"价目表存在重复模型: {item.model}")
            seen.add(key)
            currencies.add(item.currency)
        if len(currencies) > 1:
            raise ValueError(
                f"价目表必须使用同一币种，否则无法求和，当前: {sorted(currencies)}"
            )
        return self


class TokenAuditConfig(BaseSettings):
    """AI token 用量审计；只记账，不影响任何回复行为。

    关闭 enabled 即完全停用（记录直接返回、后台任务不启动），是纯 .env 回退路径。
    明细表按 detail_retention_days 自动清理，每日聚合表永久保留。
    """
    enabled: bool = True
    detail_enabled: bool = True
    detail_retention_days: int = Field(default=14, ge=1, le=365)
    flush_interval_seconds: int = Field(default=5, ge=1, le=300)
    flush_batch_size: int = Field(default=200, ge=1, le=2000)
    queue_capacity: int = Field(default=2000, ge=100, le=100000)
    purge_interval_seconds: int = Field(default=3600, ge=60, le=86400)


class DanmakuConfig(BaseSettings):
    """弹幕配置"""
    max_history: int = Field(default=50, description="最大历史弹幕数量")
    message_rate_limit: int = Field(default=10, description="消息频率限制(条/分钟)")
    enable_filter: bool = Field(default=False, description="是否启用敏感词过滤")
    
    # 弹幕池配置
    time_window_minutes: int = Field(default=5, description="弹幕时间窗口阈值(分钟)")
    frequency_threshold: int = Field(default=20, description="弹幕频率阈值(条/分钟)")
    max_unread_pool_size: int = Field(default=100, description="未读弹幕池最大容量")
    
    # 弹幕选择器权重配置
    selector_weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "content_relevance": 0.3,      # 内容相关性权重
            "sender_level": 0.2,            # 发送者等级权重
            "emotional_match": 0.25,        # 情感匹配度权重
            "timeliness": 0.15,             # 时效性权重
            "persona_consistency": 0.1      # 人格一致性权重
        },
        description="弹幕选择器权重配置"
    )
    
    # 弹幕记忆配置
    memory_max_user_danmaku: int = Field(default=50, description="每个用户最多记忆的弹幕数")
    memory_topic_decay_time: int = Field(default=5, description="话题衰减时间(分钟)")
    memory_user_inactive_time: int = Field(default=30, description="用户不活跃时间(分钟)")
    memory_time_window_size: int = Field(default=500, description="时间窗口大小")
    memory_max_topic_keywords: int = Field(default=5, description="每个弹幕最多提取的关键词数")
    memory_max_topic_memories: int = Field(default=20, description="每个话题最多记忆的弹幕数")


class PersonaDynamicsConfig(BaseModel):
    """P22 人格三轴动力学的受限调参面；可在进程启动时切换回退档案。"""

    profile: Literal["enhanced", "legacy"] = Field(
        default="enhanced",
        description="enhanced 使用场次锚点/余波；legacy 回退 P22.31 前的恢复形状",
    )
    max_step_mood: float = Field(default=0.08, gt=0, le=1)
    max_step_stress: float = Field(default=0.10, gt=0, le=1)
    max_step_darkness: float = Field(default=0.07, gt=0, le=1)
    recovery_rate_mood: float = Field(default=0.012, ge=0, le=1)
    recovery_rate_stress: float = Field(default=0.018, ge=0, le=1)
    recovery_rate_darkness: float = Field(default=0.010, ge=0, le=1)
    recovery_time_reference_seconds: float = Field(default=30.0, gt=0, le=3600)
    recovery_time_factor_max: float = Field(default=3.0, ge=1, le=100)
    repeated_topic_multiplier: float = Field(default=0.65, ge=0, le=1)
    boundary_factor_min: float = Field(default=0.35, ge=0, le=1)
    mean_reversion_threshold: float = Field(default=0.42, ge=0, lt=0.5)
    mean_reversion_strength: float = Field(default=0.25, ge=0, le=2)
    extreme_guard_mood_floor: float = Field(default=0.08, ge=0, le=0.3)
    extreme_guard_stress_ceiling: float = Field(default=0.92, ge=0.7, le=1)
    extreme_guard_darkness_ceiling: float = Field(default=0.92, ge=0.7, le=1)
    extreme_guard_recovery_multiplier: float = Field(default=1.08, ge=1, le=2)
    silence_min_activity_seconds: float = Field(default=30.0, ge=0, le=3600)
    silence_factor_reference_seconds: float = Field(default=30.0, gt=0, le=3600)
    silence_factor_max: float = Field(default=4.0, ge=1, le=100)
    silence_recovery_mood: float = Field(default=0.006, ge=0, le=1)
    silence_recovery_stress: float = Field(default=0.009, ge=0, le=1)
    silence_recovery_darkness: float = Field(default=0.005, ge=0, le=1)
    silence_max_delta_mood: float = Field(default=0.02, gt=0, le=1)
    silence_max_delta_stress: float = Field(default=0.025, gt=0, le=1)
    silence_max_delta_darkness: float = Field(default=0.015, gt=0, le=1)
    silence_cold_room_seconds: float = Field(default=120.0, ge=0, le=86400)
    silence_cold_room_mood_delta: float = Field(default=-0.0015, ge=-1, le=1)
    silence_cold_room_stress_delta: float = Field(default=0.001, ge=-1, le=1)
    anchor_min_room_samples: int = Field(default=6, ge=1, le=1000)
    anchor_room_mood_max: float = Field(default=0.04, ge=0, le=0.2)
    anchor_room_stress_max: float = Field(default=0.025, ge=0, le=0.2)
    anchor_room_darkness_max: float = Field(default=0.015, ge=0, le=0.2)
    anchor_load_stress_max: float = Field(default=0.03, ge=0, le=0.2)
    anchor_load_rate_reference: int = Field(default=30, ge=1, le=10000)
    anchor_update_min_delta: float = Field(default=0.01, gt=0, le=0.2)
    anchor_max_updates_per_stream: int = Field(default=24, ge=1, le=1000)
    afterglow_enabled: bool = Field(default=True)
    afterglow_half_life_seconds: float = Field(default=180.0, gt=1, le=86400)
    afterglow_apply_ratio: float = Field(default=0.20, ge=0, le=1)
    afterglow_capture_ratio: float = Field(default=0.40, ge=0, le=1)
    afterglow_max_mood: float = Field(default=0.05, ge=0, le=0.2)
    afterglow_max_stress: float = Field(default=0.06, ge=0, le=0.2)
    afterglow_max_darkness: float = Field(default=0.05, ge=0, le=0.2)
    afterglow_positive_relief_multiplier: float = Field(default=0.45, ge=0, le=1)
    repeated_event_decay: float = Field(default=0.15, ge=0, le=0.5)
    repeated_event_min_scale: float = Field(default=0.45, gt=0, le=1)


class PersonaConfig(BaseSettings):
    """人格配置"""
    streamer_name: str = Field(default="超天酱", description="主播名称")
    theme: str = Field(default="粉色系", description="直播主题")
    initial_mood: float = Field(default=0.6, description="初始心情值")
    initial_darkness: float = Field(default=0.2, description="初始阴暗度")
    initial_stress: float = Field(default=0.3, description="初始压力值")
    reply_aggressiveness: float = Field(default=0.4, description="回复激进程度")
    ignore_probability: float = Field(default=0.1, description="忽略弹幕概率")
    reply_plan_injection_enabled: bool = Field(
        default=True,
        description="是否向主回复提示词注入确定性 ReplyPlan；关闭时保留旧提示词链路",
    )
    prompt_mode: Literal["legacy", "shadow", "catalog"] = Field(
        default="legacy",
        description="人格 Prompt 模式；shadow 只比较不改变发送给模型的 Prompt",
    )
    prompt_rollout_percent: int = Field(
        default=0,
        ge=0,
        le=100,
        description="catalog 模式按场次稳定灰度比例；0 表示仍全部使用 legacy",
    )
    catalog_evidence_limit: int = Field(
        default=3,
        ge=1,
        le=5,
        description="结构化 Persona knowledge 每轮最多注入条数",
    )
    catalog_exemplar_enabled: bool = Field(
        default=False,
        description="是否允许检索最多一条受控风格 exemplar；默认关闭",
    )
    catalog_exemplar_limit: int = Field(
        default=1,
        ge=0,
        le=2,
        description="受控风格 exemplar 每轮最多注入条数",
    )
    emotion_diversity_enabled: bool = Field(
        default=True,
        description=(
            "是否启用情绪覆盖策略：推荐列表跨类别轮流取、冷门标定加权、"
            "重复判定收紧到近 10 次出现 2 次。关闭时回到旧的纯线性打分"
        ),
    )
    emotion_cold_bonus: float = Field(
        default=1.5,
        ge=0.0,
        le=3.0,
        description=(
            "长时间未使用的情绪在推荐打分里的最大加成；需要大于基础分差（约 1.2）"
            "才能让零加成的中性动作抢到推荐列表头部，0 表示不考虑冷门程度"
        ),
    )
    dynamics: PersonaDynamicsConfig = Field(
        default_factory=PersonaDynamicsConfig,
        description="人格三轴动力学调参；默认值兼容 P22.31 前的行为",
    )
    
    # WebSocket推送配置
    mood_push_interval_ms: int = Field(default=1000, description="心情数值推送间隔(毫秒)")
    enable_mood_push: bool = Field(default=True, description="是否启用心情数值实时推送")


class PluginConfig(BaseSettings):
    """插件配置"""
    enabled_plugins: list[str] = Field(default_factory=list, description="启用的插件列表")
    plugin_dir: str = Field(default="plugins", description="插件目录")


class StreamConfig(BaseSettings):
    """直播排期配置；具体格式错误由排期服务安全降级。"""
    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区")
    weekly_schedule: Dict[str, Any] = Field(
        default_factory=dict,
        description="每周直播时段，键为 monday-sunday，值为 start/end 数组",
    )
    daily_themes: list[Dict[str, Any]] = Field(
        default_factory=list,
        description="每日直播主题列表，包含稳定 id、展示 name 和可选 prompt_hint",
    )
    special_date_themes: list[Dict[str, Any]] = Field(
        default_factory=list,
        description="按本地月日重复匹配的特殊日期主题；仅作为每日主题的人格 bias",
    )
    activity_candidates: list[Dict[str, Any]] = Field(
        default_factory=lambda: [
            {"id": "internet-chat", "category": "chat", "name": "互联网杂谈", "object_name": "网络见闻", "theme_ids": ["internet-chat", "viewer-day", "angel-advice", "late-night-radio"], "min_duration_minutes": 30},
            {"id": "super-mario", "category": "game", "name": "游戏实况", "object_name": "超级马里奥", "theme_ids": ["game-night"], "min_duration_minutes": 45},
            {"id": "music-chat", "category": "music", "name": "音乐闲聊", "object_name": "最近循环的歌", "theme_ids": ["music-wave"], "min_duration_minutes": 30},
            {"id": "random-plan", "category": "variety", "name": "随机企划", "object_name": "即兴挑战", "theme_ids": ["chaos-plan"], "min_duration_minutes": 30},
            {"id": "free-chat", "category": "chat", "name": "轻松杂谈", "object_name": "和宅宅们聊天", "theme_ids": ["*"], "min_duration_minutes": 30},
        ],
        description="主播活动候选目录；theme_ids 使用每日主题 ID 或 * 兜底",
    )
    activity_evaluation_interval_seconds: int = Field(default=60, ge=5, le=3600)
    activity_switch_cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    activity_max_duration_minutes: int = Field(default=120, ge=5, le=1440)
    activity_busy_rate_threshold: int = Field(default=40, ge=1, le=10000)
    activity_suggestion_min_familiarity: float = Field(default=0.4, ge=0, le=1)
    activity_suggestion_min_trust: float = Field(default=0.5, ge=0, le=1)
    activity_public_performance_enabled: bool = True
    activity_public_performance_min_interval_minutes: int = Field(default=30, ge=1, le=1440)
    activity_public_performance_max_per_stream: int = Field(default=6, ge=0, le=100)
    beat_enabled: bool = Field(default=False, description="是否启用独立主播微动作；默认关闭且不调用 AI")
    beat_evaluation_interval_seconds: int = Field(default=30, ge=5, le=3600)
    beat_min_interval_seconds: int = Field(default=120, ge=10, le=86400)
    beat_max_per_stream: int = Field(default=6, ge=0, le=100)
    beat_low_activity_max_rate: int = Field(default=3, ge=0, le=10000)
    beat_slow_consumer_queue_threshold: int = Field(default=1, ge=1, le=1000)
    mainline_enabled: bool = Field(
        default=True,
        description="主线事实层（Plan 快照与当前 Beat 的持久化及公开字段）；关闭即回到无主线行为，Director 随之空转",
    )
    mainline_theme_projection_enabled: bool = Field(
        default=False,
        description="直播中用本场冻结快照改写公开的 daily_theme_*；默认关闭，旧字段仍跟随实时配置",
    )
    mainline_prompt_injection_enabled: bool = Field(
        default=False,
        description="向人格 Prompt 注入本场主线事实；默认关闭，与回复计划注入同样按灰度开启",
    )
    director_enabled: bool = Field(
        default=False,
        description="启用直播主线 Director 运行时；默认关闭，事实层仍正常工作",
    )
    director_mode: Literal["shadow", "deterministic", "ai_shadow", "ai"] = Field(
        default="shadow",
        description="Director 灰度模式；shadow 永不改写事实",
    )
    director_activity_driver: Literal["legacy", "director"] = Field(
        default="legacy",
        description="自主 Activity 驱动；验证前保持 legacy",
    )
    director_tick_seconds: int = Field(default=300, ge=30, le=3600)
    director_queue_capacity: int = Field(default=32, ge=4, le=256)
    director_trigger_coalesce_seconds: int = Field(default=30, ge=1, le=300)
    director_opening_min_seconds: int = Field(default=120, ge=10, le=3600)
    director_detour_return_min_seconds: int = Field(default=600, ge=60, le=14400)
    director_quiet_seconds: int = Field(default=180, ge=30, le=3600)
    director_wrap_up_seconds: int = Field(default=600, ge=60, le=3600)
    director_fact_cooldown_seconds: int = Field(default=300, ge=30, le=3600)
    director_public_action_cooldown_seconds: int = Field(default=300, ge=30, le=3600)
    director_speak_cooldown_seconds: int = Field(default=900, ge=60, le=7200)
    director_max_public_actions_per_stream: int = Field(default=8, ge=0, le=100)
    director_max_speaks_per_stream: int = Field(default=3, ge=0, le=50)
    director_performance_enabled: bool = Field(
        default=False,
        description="允许确定性模板 Speak/Animation；默认关闭",
    )
    director_ai_min_interval_seconds: int = Field(default=900, ge=60, le=14400)
    director_ai_max_per_stream: int = Field(default=3, ge=0, le=50)
    director_ai_rollout_percent: int = Field(
        default=0, ge=0, le=100,
        description="按 stream_session_id 稳定灰度 AI Director；0 表示完全关闭",
    )
    director_ai_speak_polish_enabled: bool = Field(
        default=False,
        description="极少数主动模板台词允许 AI 单句润色；默认关闭",
    )
    idle_state_min_duration_seconds: int = Field(default=60, ge=0, le=3600)
    language_detection_min_confidence: float = Field(default=0.65, ge=0.5, le=1.0)
    language_detection_min_script_chars: int = Field(default=3, ge=1, le=20)
    english_surprise_joke_enabled: bool = Field(
        default=True,
        description="英文观众首次互动时可选一次性互动梗",
    )
    english_surprise_joke_probability: float = Field(default=0.35, ge=0.0, le=1.0)
    english_surprise_joke_max_per_stream: int = Field(default=3, ge=0, le=100)


class SessionSummaryConfig(BaseSettings):
    """P21 场次总结的受控开关与资源边界。"""

    capture_enabled: bool = Field(
        default=True,
        description="按排期边界冻结脱敏场次事实并创建可恢复任务",
    )
    ai_enabled: bool = Field(
        default=False,
        description="显式开启后才允许低优先级总结任务调用外部 AI",
    )
    max_attempts: int = Field(default=2, ge=1, le=5)
    processing_lease_seconds: int = Field(default=300, ge=30, le=3600)
    poll_interval_seconds: float = Field(default=5.0, ge=0.5, le=300.0)
    ai_timeout_seconds: int = Field(default=45, ge=5, le=300)
    prompt_chars: int = Field(default=180, ge=40, le=600)
    retention_days: int = Field(default=30, ge=1, le=3650)


class AuthConfig(BaseSettings):
    """账号认证配置。"""
    access_token_ttl_hours: int = Field(default=168, ge=1, le=2160)
    refresh_token_ttl_hours: int = Field(default=720, ge=1, le=8760)
    min_password_length: int = Field(default=8, ge=8, le=128)
    cookie_name: str = "kangel_access_token"
    refresh_cookie_name: str = "kangel_refresh_token"
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: Optional[str] = None
    cookie_partitioned: bool = False

    @field_validator("cookie_domain", mode="before")
    @classmethod
    def normalize_cookie_domain(cls, value):
        normalized = str(value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_cookie_policy(self) -> "AuthConfig":
        if self.refresh_cookie_name == self.cookie_name:
            raise ValueError("refresh Cookie 名称必须与 access Cookie 不同")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("SameSite=None 必须同时启用 Secure Cookie")
        if self.cookie_partitioned and (
            not self.cookie_secure or self.cookie_samesite != "none"
        ):
            raise ValueError("Partitioned Cookie 必须同时使用 Secure 和 SameSite=None")
        return self


class CORSConfig(BaseSettings):
    """浏览器跨域访问白名单；启用凭据时禁止使用通配来源。"""
    allowed_origins: list[str] = Field(default_factory=lambda: [
        "https://kotorin-kawaii.github.io",
        "https://kangel.kotorin.cn",
        "http://localhost:5173",
        "http://localhost:3000",
    ])
    allow_credentials: bool = True
    max_age_seconds: int = Field(default=600, ge=0, le=86400)

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, value: list[str]) -> list[str]:
        origins = [str(item).strip().rstrip("/") for item in value if str(item).strip()]
        if not origins or "*" in origins:
            raise ValueError("CORS 来源必须是非空精确白名单，不能使用通配符")
        if any(not origin.startswith(("http://", "https://")) for origin in origins):
            raise ValueError("CORS 来源必须包含 http:// 或 https:// 协议")
        return list(dict.fromkeys(origins))


class AdminConfig(BaseSettings):
    """敏感调试与控制接口；默认关闭且不复用用户登录令牌。"""
    enabled: bool = False
    api_key: SecretStr = SecretStr("")
    rate_per_minute: float = Field(default=30.0, gt=0)
    burst: int = Field(default=10, ge=1)
    concurrency: int = Field(default=2, ge=1, le=32)


class MemoryConfig(BaseSettings):
    """登录用户长期人物记忆的隐私与保留策略。"""
    enabled_by_default: bool = True
    retention_days: int = Field(default=180, ge=1, le=3650)
    max_text_length: int = Field(default=500, ge=50, le=5000)
    recent_fragment_limit: int = Field(default=8, ge=2, le=30)
    retrieval_limit: int = Field(default=6, ge=1, le=20)
    compact_after_fragments: int = Field(default=12, ge=4, le=100)
    summary_max_chars: int = Field(default=600, ge=100, le=3000)
    importance_half_life_days: int = Field(default=45, ge=1, le=3650)
    max_archived_fragments: int = Field(default=100, ge=10, le=5000)
    prompt_fragment_limit: int = Field(default=3, ge=1, le=10)
    prompt_summary_limit: int = Field(default=1, ge=0, le=5)
    prompt_fragment_chars: int = Field(default=160, ge=50, le=500)
    prompt_summary_chars: int = Field(default=240, ge=50, le=1000)


class ViewerImpressionConfig(BaseSettings):
    """注册用户的低频长期印象留言；独立于实时回复链。"""

    enabled: bool = False
    cooldown_days: int = Field(default=7, ge=1, le=365)
    min_conversation_fragments: int = Field(default=3, ge=0, le=100)
    min_topic_memories: int = Field(default=1, ge=0, le=100)
    max_fragment_evidence: int = Field(default=12, ge=0, le=100)
    max_topic_evidence: int = Field(default=8, ge=0, le=100)
    max_episodic_evidence: int = Field(default=5, ge=0, le=100)
    max_prompt_chars: int = Field(default=12000, ge=1000, le=50000)
    # Deep Reflection v2: independent, bounded history and per-stage budgets.
    # The legacy evidence/prompt limits above apply only to frozen v1 tasks.
    max_fragment_candidates: int = Field(default=500, ge=1, le=2000)
    max_topic_candidates: int = Field(default=100, ge=1, le=2000)
    max_episodic_candidates: int = Field(default=100, ge=1, le=2000)
    max_nickname_history: int = Field(default=50, ge=1, le=500)
    archaeologist_max_prompt_chars: int = Field(default=600000, ge=8000, le=4000000)
    synthesizer_max_prompt_chars: int = Field(default=80000, ge=8000, le=500000)
    writer_max_prompt_chars: int = Field(default=40000, ge=8000, le=500000)
    critic_max_prompt_chars: int = Field(default=80000, ge=8000, le=500000)
    max_repair_passes: int = Field(default=1, ge=0, le=1)
    max_archaeology_chunks: int = Field(default=256, ge=1, le=1024)
    stage_output_chars: int = Field(default=8000, ge=1000, le=80000)
    allow_v1_fallback: bool = False
    allow_without_critic: bool = False
    max_output_chars: int = Field(default=1800, ge=100, le=10000)
    worker_concurrency: int = Field(default=1, ge=1, le=4)
    max_pending_tasks: int = Field(default=100, ge=1, le=10000)
    max_attempts: int = Field(default=3, ge=1, le=8)
    processing_lease_seconds: int = Field(default=600, ge=30, le=3600)
    retry_backoff_seconds: int = Field(default=600, ge=0, le=86400)
    retry_backoff_max_seconds: int = Field(default=3600, ge=1, le=604800)


class EpisodicMemoryConfig(BaseSettings):
    """P24 主播情景记忆；与 P21 公共场次总结分离。"""

    enabled: bool = True
    ai_enabled: bool = False
    max_attempts: int = Field(default=3, ge=1, le=8)
    processing_lease_seconds: int = Field(default=300, ge=30, le=3600)
    poll_interval_seconds: float = Field(default=5.0, ge=0.5, le=300.0)
    ai_timeout_seconds: int = Field(default=60, ge=5, le=300)
    candidate_min_importance: float = Field(default=0.75, ge=0.0, le=1.0)
    appraisal_min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    appraisal_min_novelty: float = Field(default=0.45, ge=0.0, le=1.0)
    max_candidates_per_session: int = Field(default=48, ge=1, le=500)
    # 单次模型调用的硬上限；一场直播可以拆成多个批次。
    batch_size: int = Field(default=8, ge=1, le=32)
    max_batches_per_task: int = Field(default=32, ge=1, le=128)
    retry_backoff_seconds: float = Field(default=30.0, ge=0, le=86400)
    retry_backoff_max_seconds: float = Field(default=1800.0, ge=1, le=604800)
    reconciliation_batch_size: int = Field(default=100, ge=1, le=1000)
    error_detail_max_chars: int = Field(default=320, ge=80, le=2000)
    max_candidates_per_account: int = Field(default=5, ge=1, le=50)
    max_memories_per_session: int = Field(default=12, ge=1, le=100)
    max_memories_per_account: int = Field(default=3, ge=1, le=20)
    account_retention_days: int = Field(default=180, ge=1, le=3650)
    room_retention_days: int = Field(default=90, ge=1, le=3650)
    reflection_retention_days: int = Field(default=90, ge=1, le=3650)
    retrieval_account_limit: int = Field(default=2, ge=0, le=10)
    retrieval_room_limit: int = Field(default=1, ge=0, le=5)
    retrieval_prompt_chars: int = Field(default=500, ge=100, le=2000)
    source_excerpt_chars: int = Field(default=180, ge=40, le=500)
    backfill_batch_size: int = Field(default=250, ge=1, le=5000)
    backfill_timezone: str = "Asia/Shanghai"
    backfill_stream_start_hour: int = Field(default=6, ge=0, le=23)
    backfill_stream_end_hour: int = Field(default=3, ge=0, le=23)
    # 只有该日期及之后已有真实 danmaku_id 关联的历史数据才允许按
    # account_id 回填；更早数据一律降级为匿名房间记忆。
    backfill_account_link_start_date: str = "2026-07-30"


class PromptRamConfig(BaseSettings):
    """P30 主播工作记忆（prompt RAM）：秒/分钟级、场次内、未闭合的意图。

    与 P24 情景记忆的分工：情景记忆记「已经发生的事实」，跨场次、话题命中才召回；
    prompt RAM 记「自己还没闭合的念头」，只在本场次内存活，活着就注入。
    纯进程内、易失，重启与换场次一律清空。
    """

    enabled: bool = False
    note_max_chars: int = Field(default=80, ge=20, le=200)
    max_entries: int = Field(default=24, ge=1, le=200)
    inject_max_entries: int = Field(default=4, ge=1, le=12)
    harvest_max_per_reply: int = Field(default=2, ge=1, le=8)
    awaiting_ttl_seconds: float = Field(default=180.0, ge=15, le=1800)
    followup_ttl_seconds: float = Field(default=300.0, ge=15, le=3600)
    idea_ttl_seconds: float = Field(default=420.0, ge=15, le=3600)
    # 被等的人开口之后仍然注入一小段时间，好让本轮回复能自然接上「你等的回话来了」。
    fulfilled_grace_seconds: float = Field(default=45.0, ge=5, le=300)
    # 弹幕筛选的本地打分加成。并发闸门满时筛选会绕过 AI 直接取第一名，
    # 只改提示词的话「等待」在那条路径上完全失效。
    selector_bonus: float = Field(default=0.15, ge=0.0, le=0.5)
    purge_interval_seconds: float = Field(default=30.0, ge=5, le=600)


class SCConfig(BaseSettings):
    cooldown_seconds: int = Field(default=300, ge=1, le=86400)
    max_content_chars: int = Field(default=500, ge=1, le=5000)
    max_content_bytes: int = Field(default=2048, ge=64, le=65536)
    max_pending_items: int = Field(default=100, ge=1, le=10000)
    poll_interval_seconds: float = Field(default=0.5, ge=0.1, le=60)
    processing_lease_seconds: int = Field(default=300, ge=30, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=20)
    estimated_processing_seconds: int = Field(default=30, ge=1, le=3600)
    submit_ip_rate_per_minute: float = Field(default=12.0, gt=0)
    submit_ip_burst: int = Field(default=4, ge=1, le=100)
    submit_account_rate_per_minute: float = Field(default=6.0, gt=0)
    submit_account_burst: int = Field(default=2, ge=1, le=100)
    submit_global_rate_per_minute: float = Field(default=120.0, gt=0)
    submit_global_burst: int = Field(default=30, ge=1, le=1000)
    reject_prompt_injection: bool = True
    blocked_terms: list[str] = Field(default_factory=list)


class SponsorConfig(BaseSettings):
    """P25 自愿赞助入口与赞助者感谢墙。

    赞助不授予任何功能权益：不给权限、不给 SC 额度、不给徽章、不给排队优先级，
    也不进入人格/记忆链路。它只影响页面底部的展示。

    afdian_user_id / afdian_token 是爱发电开放 API 凭据，仅在服务端使用，
    禁止出现在任何 HTTP 响应或日志中。
    """

    enabled: bool = False
    platform_name: str = "爱发电"
    platform_url: str = ""
    notice_text: str = (
        "AI 接口成本涨了不少，为爱发电有点吃力。赞助完全自愿，"
        "不解锁任何功能，也不会影响弹幕、SC 或回复优先级。"
    )
    afdian_user_id: str = ""
    afdian_token: str = ""
    sync_enabled: bool = False
    sync_interval_seconds: int = Field(default=900, ge=60, le=86400)
    sync_timeout_seconds: int = Field(default=15, ge=3, le=60)
    sync_max_pages: int = Field(default=20, ge=1, le=200)
    sync_backoff_seconds: int = Field(default=60, ge=10, le=3600)
    sync_max_backoff_seconds: int = Field(default=3600, ge=60, le=86400)
    # Sponsor Fund Transparency：与感谢墙完全隔离，默认关闭。
    transparency_enabled: bool = False
    finance_sync_enabled: bool = False
    finance_sync_interval_seconds: int = Field(default=3600, ge=300, le=86400)
    finance_sync_max_pages: int = Field(default=20, ge=1, le=200)
    transparency_cache_seconds: int = Field(default=60, ge=0, le=3600)
    list_limit: int = Field(default=200, ge=1, le=2000)
    list_cache_seconds: int = Field(default=60, ge=0, le=3600)
    max_display_name_chars: int = Field(default=24, ge=1, le=64)
    anonymous_display_name: str = "匿名赞助者"
    anonymous_keywords: list[str] = Field(
        default_factory=lambda: ["匿名", "不上墙", "anonymous"]
    )
    hidden_platform_user_ids: list[str] = Field(default_factory=list)

    @field_validator("platform_url")
    @classmethod
    def validate_platform_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("SPONSOR__PLATFORM_URL 必须是 http(s) 链接")
        return value

    @model_validator(mode="after")
    def validate_sync_credentials(self):
        if (self.sync_enabled or self.finance_sync_enabled) and not (
            self.afdian_user_id.strip() and self.afdian_token.strip()
        ):
            raise ValueError(
                "开启 SPONSOR__SYNC_ENABLED 或 SPONSOR__FINANCE_SYNC_ENABLED 必须同时配置 "
                "SPONSOR__AFDIAN_USER_ID 与 SPONSOR__AFDIAN_TOKEN"
            )
        return self

    @property
    def list_enabled(self) -> bool:
        """感谢墙是否对外可见：总开关与同步开关都打开才展示名单。"""
        return self.enabled and self.sync_enabled


class ModerationConfig(BaseSettings):
    """主播管理系统配置；LLM 只提出语义建议，后端负责执行与持久化。"""

    enabled: bool = True
    analysis_enabled: bool = True
    max_pending_tasks: int = Field(default=64, ge=1, le=512)
    reservation_ttl_seconds: int = Field(default=120, ge=15, le=3600)
    recent_message_limit: int = Field(default=8, ge=1, le=30)
    state_retention_days: int = Field(default=30, ge=1, le=3650)
    decay_per_minute: float = Field(default=2.0, ge=0.0, le=20.0)
    violation_window_minutes: int = Field(default=5, ge=1, le=1440)
    warning_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    timeout_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    admin_review_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    relation_relief_max: float = Field(default=0.20, ge=0.0, le=0.5)
    hard_violation_terms: list[str] = Field(default_factory=lambda: [
        "人肉", "地址", "手机号", "杀了你", "弄死你", "炸死你",
    ])
    stream_profiles: Dict[str, Dict[str, float]] = Field(default_factory=lambda: {
        "default": {"warning": 0.60, "timeout": 0.80, "admin_review": 0.95},
        "special_event": {"warning": 0.55, "timeout": 0.75, "admin_review": 0.92},
    })


class EmoteConfig(BaseSettings):
    """纯展示观众表情；目录只维护稳定 ID，不包含前端静态资源路径。"""
    allowed_ids: list[str] = Field(default_factory=lambda: [
        "heart", "clap", "laugh", "surprised", "sad", "angry", "cheer",
    ])
    cooldown_seconds: int = Field(default=3, ge=1, le=3600)
    dedup_ttl_seconds: int = Field(default=60, ge=1, le=86400)
    connection_rate_per_minute: float = Field(default=60.0, gt=0)
    connection_burst: int = Field(default=10, ge=1, le=1000)
    ip_rate_per_minute: float = Field(default=120.0, gt=0)
    ip_burst: int = Field(default=30, ge=1, le=5000)
    global_rate_per_minute: float = Field(default=1200.0, gt=0)
    global_burst: int = Field(default=200, ge=1, le=10000)

    @field_validator("allowed_ids")
    @classmethod
    def validate_allowed_ids(cls, value: list[str]) -> list[str]:
        pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
        normalized = [str(item).strip() for item in value]
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("表情 ID 列表不能为空或重复")
        if any(not pattern.fullmatch(item) for item in normalized):
            raise ValueError("表情 ID 只能包含字母、数字、下划线和连字符")
        return normalized


class RateLimitConfig(BaseSettings):
    """单 Python 进程的应用内限流；持久业务状态使用 SQLite。"""
    profile: Literal["development", "trusted", "public"] = "public"
    local_test_relaxed: bool = False
    enabled: bool = True
    register_ip_rate_per_minute: float = Field(default=3.0, gt=0)
    register_ip_burst: int = Field(default=3, ge=1)
    register_subnet_rate_per_minute: float = Field(default=12.0, gt=0)
    register_subnet_burst: int = Field(default=8, ge=1)
    register_global_rate_per_minute: float = Field(default=60.0, gt=0)
    register_global_burst: int = Field(default=20, ge=1)
    login_ip_rate_per_minute: float = Field(default=20.0, gt=0)
    login_ip_burst: int = Field(default=8, ge=1)
    login_username_rate_per_minute: float = Field(default=5.0, gt=0)
    login_username_burst: int = Field(default=3, ge=1)
    login_global_rate_per_minute: float = Field(default=120.0, gt=0)
    login_global_burst: int = Field(default=30, ge=1)
    rejection_cooldown_seconds: int = Field(default=5, ge=0, le=3600)
    register_hash_concurrency: int = Field(default=2, ge=1, le=64)
    login_hash_concurrency: int = Field(default=4, ge=1, le=128)
    login_failure_threshold: int = Field(default=3, ge=1, le=20)
    login_failure_base_cooldown_seconds: int = Field(default=2, ge=1, le=3600)
    login_failure_max_cooldown_seconds: int = Field(default=60, ge=1, le=86400)
    password_change_ip_rate_per_minute: float = Field(default=6.0, gt=0)
    password_change_ip_burst: int = Field(default=3, ge=1)
    password_change_account_rate_per_minute: float = Field(default=6.0, gt=0)
    password_change_account_burst: int = Field(default=3, ge=1)
    password_change_global_rate_per_minute: float = Field(default=60.0, gt=0)
    password_change_global_burst: int = Field(default=20, ge=1)
    password_change_hash_concurrency: int = Field(default=2, ge=1, le=64)
    password_change_failure_threshold: int = Field(default=3, ge=1, le=20)
    password_change_failure_base_cooldown_seconds: int = Field(default=5, ge=1, le=3600)
    password_change_failure_max_cooldown_seconds: int = Field(default=300, ge=1, le=86400)
    profile_read_rate_per_minute: float = Field(default=60.0, gt=0)
    profile_read_burst: int = Field(default=20, ge=1)
    profile_write_rate_per_minute: float = Field(default=10.0, gt=0)
    profile_write_burst: int = Field(default=5, ge=1)
    http_ip_rate_per_minute: float = Field(default=300.0, gt=0)
    http_ip_burst: int = Field(default=100, ge=1)
    http_global_rate_per_minute: float = Field(default=3000.0, gt=0)
    http_global_burst: int = Field(default=500, ge=1)
    http_max_header_bytes: int = Field(default=16384, ge=1024, le=1048576)
    http_max_query_bytes: int = Field(default=4096, ge=256, le=1048576)
    http_max_body_bytes: int = Field(default=65536, ge=1024, le=10485760)
    http_body_read_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    http_request_timeout_seconds: float = Field(default=30.0, gt=0, le=1800)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)
    ws_handshake_ip_rate_per_minute: float = Field(default=10.0, gt=0)
    ws_handshake_ip_burst: int = Field(default=5, ge=1)
    ws_handshake_account_rate_per_minute: float = Field(default=10.0, gt=0)
    ws_handshake_account_burst: int = Field(default=5, ge=1)
    ws_handshake_global_rate_per_minute: float = Field(default=120.0, gt=0)
    ws_handshake_global_burst: int = Field(default=30, ge=1)
    ws_max_global_connections: int = Field(default=500, ge=1)
    ws_max_ip_connections: int = Field(default=5, ge=1)
    ws_max_account_connections: int = Field(default=3, ge=1)
    ws_message_connection_rate_per_minute: float = Field(default=60.0, gt=0)
    ws_message_connection_burst: int = Field(default=10, ge=1)
    ws_message_ip_rate_per_minute: float = Field(default=120.0, gt=0)
    ws_message_ip_burst: int = Field(default=30, ge=1)
    ws_message_account_rate_per_minute: float = Field(default=90.0, gt=0)
    ws_message_account_burst: int = Field(default=20, ge=1)
    ws_message_global_rate_per_minute: float = Field(default=1000.0, gt=0)
    ws_message_global_burst: int = Field(default=200, ge=1)
    ws_max_frame_bytes: int = Field(default=4096, ge=256, le=1048576)
    ws_max_nickname_chars: int = Field(default=100, ge=1, le=500)
    ws_max_message_chars: int = Field(default=500, ge=1, le=10000)
    ws_max_danmaku_id_chars: int = Field(default=128, ge=8, le=500)
    ws_dedup_ttl_seconds: int = Field(default=300, ge=1, le=86400)
    ws_idle_timeout_seconds: int = Field(default=0, ge=0, le=86400)
    ws_max_lifetime_seconds: int = Field(default=0, ge=0, le=604800)
    ws_presence_grace_seconds: float = Field(default=5.0, ge=0, le=60)
    ws_send_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    ws_send_queue_size: int = Field(default=64, ge=1, le=10000)
    ai_selector_concurrency: int = Field(default=4, ge=1, le=128)
    ai_reply_concurrency: int = Field(default=2, ge=1, le=128)
    ai_reply_subject_rate_per_minute: float = Field(default=6.0, gt=0)
    ai_reply_subject_burst: int = Field(default=3, ge=1)
    ai_reply_ip_rate_per_minute: float = Field(default=30.0, gt=0)
    ai_reply_ip_burst: int = Field(default=10, ge=1)
    ai_reply_global_rate_per_minute: float = Field(default=120.0, gt=0)
    ai_reply_global_burst: int = Field(default=20, ge=1)
    ai_reply_queue_size: int = Field(default=8, ge=0, le=1000)
    ai_reply_queue_wait_seconds: float = Field(default=2.0, gt=0, le=60)
    ai_reply_claim_lease_seconds: int = Field(
        default=900,
        ge=60,
        le=86400,
        description="普通弹幕场次级处理权租约；仅用于崩溃后回收未完成 claim",
    )
    overload_enabled: bool = True
    overload_max_connections: int = Field(default=450, ge=1, le=100000)
    overload_max_ai_waiters: int = Field(default=8, ge=0, le=10000)
    overload_max_rss_mb: int = Field(default=0, ge=0, le=1048576)
    overload_max_cpu_load_per_core: float = Field(default=0.0, ge=0, le=100)
    overload_retry_after_seconds: int = Field(default=3, ge=1, le=300)

    @model_validator(mode="after")
    def apply_security_profile(self) -> "RateLimitConfig":
        """为未显式配置的字段应用环境档位；逐字段配置始终优先。"""
        if self.local_test_relaxed and self.profile != "development":
            raise ValueError("local_test_relaxed 只能与 development 档位同时启用")

        presets = {
            "public": {},
            "trusted": {
                "register_ip_rate_per_minute": 10.0,
                "register_ip_burst": 5,
                "login_ip_rate_per_minute": 60.0,
                "login_ip_burst": 20,
                "http_ip_rate_per_minute": 900.0,
                "http_ip_burst": 300,
                "ws_handshake_ip_rate_per_minute": 30.0,
                "ws_handshake_ip_burst": 12,
                "ws_max_ip_connections": 20,
                "ws_message_ip_rate_per_minute": 360.0,
                "ws_message_ip_burst": 90,
            },
            "development": {
                "register_ip_rate_per_minute": 60.0,
                "register_ip_burst": 20,
                "login_ip_rate_per_minute": 240.0,
                "login_ip_burst": 60,
                "http_ip_rate_per_minute": 3000.0,
                "http_ip_burst": 1000,
                "ws_handshake_ip_rate_per_minute": 120.0,
                "ws_handshake_ip_burst": 30,
                "ws_max_ip_connections": 50,
                "ws_message_ip_rate_per_minute": 1200.0,
                "ws_message_ip_burst": 300,
                "ai_selector_concurrency": 8,
                "ai_reply_concurrency": 4,
            },
        }
        explicit_fields = self.model_fields_set
        for field_name, value in presets[self.profile].items():
            if field_name not in explicit_fields:
                object.__setattr__(self, field_name, value)

        if self.local_test_relaxed:
            relaxed = {
                "register_ip_rate_per_minute": 600.0,
                "register_ip_burst": 100,
                "login_ip_rate_per_minute": 1200.0,
                "login_ip_burst": 200,
                "ws_handshake_ip_rate_per_minute": 600.0,
                "ws_handshake_ip_burst": 100,
                "ws_message_ip_rate_per_minute": 6000.0,
                "ws_message_ip_burst": 1000,
            }
            for field_name, value in relaxed.items():
                if field_name not in explicit_fields:
                    object.__setattr__(self, field_name, value)
        return self


class Settings(BaseSettings):
    """全局配置"""
    project_name: str = "虚拟主播直播系统"
    api_v1_str: str = "/api/v1"
    
    server: ServerConfig = Field(default_factory=ServerConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    token_audit: TokenAuditConfig = Field(default_factory=TokenAuditConfig)
    danmaku: DanmakuConfig = Field(default_factory=DanmakuConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    session_summary: SessionSummaryConfig = Field(default_factory=SessionSummaryConfig)
    episodic_memory: EpisodicMemoryConfig = Field(default_factory=EpisodicMemoryConfig)
    prompt_ram: PromptRamConfig = Field(default_factory=PromptRamConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    viewer_impression: ViewerImpressionConfig = Field(default_factory=ViewerImpressionConfig)
    sc: SCConfig = Field(default_factory=SCConfig)
    sponsor: SponsorConfig = Field(default_factory=SponsorConfig)
    emotes: EmoteConfig = Field(default_factory=EmoteConfig)
    moderation: ModerationConfig = Field(default_factory=ModerationConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore"
    )


settings = Settings()
