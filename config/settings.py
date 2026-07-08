from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, Dict, Any, Literal
from pydantic import Field, SecretStr, field_validator, model_validator
import re


class ServerConfig(BaseSettings):
    """服务器配置"""
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    log_level: str = "debug"


class AIConfig(BaseSettings):
    """OpenAI-compatible AI 服务配置。"""
    base_url: str = Field(default="https://api.siliconflow.cn/v1", description="OpenAI-compatible API 基础URL")
    api_key: str = Field(default="", description="API密钥")
    default_model: str = Field(default="Pro/MiniMaxAI/MiniMax-M2.5", description="默认模型")
    qa_selector_model: Optional[str] = Field(default=None, description="QA选择器模型，留空时使用默认模型")
    qa_selector_timeout: int = Field(default=20, description="QA选择器API超时时间(秒)")
    danmaku_selector_model: Optional[str] = Field(default=None, description="弹幕候选选择模型，留空时使用默认模型")
    danmaku_selector_timeout: int = Field(default=15, description="弹幕候选选择超时时间(秒)")
    impact_analysis_model: Optional[str] = Field(default=None, description="人格影响分析模型，留空时使用默认模型")
    impact_analysis_timeout: int = Field(default=30, description="人格影响分析超时时间(秒)")
    parallel_context_analysis: bool = Field(
        default=True,
        description="并行执行QA选择与人格影响分析",
    )
    temperature: float = Field(default=0.2, description="温度参数")
    streaming: bool = Field(default=False, description="是否启用流式输出")
    timeout: int = Field(default=600, description="请求超时时间(秒)")


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


class PersonaConfig(BaseSettings):
    """人格配置"""
    streamer_name: str = Field(default="超天酱", description="主播名称")
    theme: str = Field(default="粉色系", description="直播主题")
    initial_mood: float = Field(default=0.6, description="初始心情值")
    initial_darkness: float = Field(default=0.2, description="初始阴暗度")
    initial_stress: float = Field(default=0.3, description="初始压力值")
    reply_aggressiveness: float = Field(default=0.4, description="回复激进程度")
    ignore_probability: float = Field(default=0.1, description="忽略弹幕概率")
    
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


class AuthConfig(BaseSettings):
    """账号认证配置。"""
    access_token_ttl_hours: int = Field(default=168, ge=1, le=2160)
    min_password_length: int = Field(default=8, ge=8, le=128)
    cookie_name: str = "kangel_access_token"
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
    danmaku: DanmakuConfig = Field(default_factory=DanmakuConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    sc: SCConfig = Field(default_factory=SCConfig)
    emotes: EmoteConfig = Field(default_factory=EmoteConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore"
    )


settings = Settings()
