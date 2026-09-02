import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import SecretStr

from .settings import Settings, settings


class ConfigManager:
    """配置管理器，支持多配置源"""

    # Viewer Impression owns an asyncio worker pool which is created during
    # FastAPI lifespan.  Treating its settings as hot-reloadable would leave
    # an enabled queue without workers (or keep workers running after disable).
    # Edit the config/.env and restart the service for these values to apply.
    RESTART_REQUIRED_SECTIONS = frozenset({"viewer_impression"})

    # 导出时替换凭据的固定哨兵；回灌时会跳过恰好等于它的值。
    REDACTED = "***"
    SECRET_KEY_MARKERS = ("api_key", "apikey", "token", "secret", "password")

    def __init__(self, config_file: Optional[str] = None):
        self._config_file = config_file or "config.json"
        self._config_cache: Optional[Dict[str, Any]] = None
        self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        # 延迟导入logger避免循环导入
        from kangel.shared.logging import logger
        
        if os.path.exists(self._config_file):
            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
                    self._config_cache = json.load(f)
                logger.info(f"配置文件加载成功: {self._config_file}")
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")
                self._config_cache = {}
        else:
            self._config_cache = {}
    
    def save_config(self):
        """保存配置到文件"""
        from kangel.shared.logging import logger
        
        try:
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(self._config_cache, f, ensure_ascii=False, indent=2)
            logger.info(f"配置文件保存成功: {self._config_file}")
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        if self._config_cache and key in self._config_cache:
            return self._config_cache[key]
        return default
    
    def set(self, key: str, value: Any):
        """设置配置值"""
        section = str(key).split(".", 1)[0]
        if section in self.RESTART_REQUIRED_SECTIONS:
            raise RuntimeError(
                "viewer_impression 配置需要重启服务后生效，不能通过运行时配置接口热更新"
            )
        if self._config_cache is None:
            self._config_cache = {}
        self._config_cache[key] = value
        self.save_config()
    
    def update_settings(self, settings_obj: Settings) -> Settings:
        """从配置文件更新Settings对象"""
        from kangel.shared.logging import logger

        if not self._config_cache:
            return settings_obj

        try:
            for section in (
                "server", "ai", "danmaku", "persona", "stream", "memory",
                "viewer_impression", "cors", "sc", "emotes", "admin", "rate_limit",
            ):
                source = self._config_cache.get(section)
                if not isinstance(source, dict):
                    continue
                target = getattr(settings_obj, section, None)
                if target is None:
                    continue
                for key, value in source.items():
                    # 跳过脱敏哨兵：有人 GET /config 再 PUT 回来时，
                    # 不能把 "***" 当成真密钥写进运行配置。
                    if self._is_redacted(value):
                        logger.warning(
                            "跳过 %s.%s：值是脱敏哨兵，不覆盖真实凭据", section, key
                        )
                        continue
                    if hasattr(target, key):
                        setattr(target, key, value)

            logger.info("Settings从配置文件更新成功")
        except Exception as e:
            logger.error(f"更新Settings失败: {e}")

        return settings_obj

    @classmethod
    def _is_redacted(cls, value: Any) -> bool:
        return isinstance(value, str) and value == cls.REDACTED
    
    def export_config(self) -> Dict[str, Any]:
        """导出完整配置（密钥已脱敏）。

        这份 JSON 会在管理后台的浏览器里渲染，所以凭据必须在服务端就换成哨兵。
        `sponsor` 与 `token_audit` 刻意不在白名单里：前者含爱发电凭据，
        后者的开关通过 `/admin/tokens/stats` 展示，暴露面越小越好。
        """
        return self._redact_secrets({
            "server": settings.server.model_dump(),
            "ai": settings.ai.model_dump(),
            "danmaku": settings.danmaku.model_dump(),
            "persona": settings.persona.model_dump(),
            "plugins": settings.plugins.model_dump(),
            "stream": settings.stream.model_dump(),
            "memory": settings.memory.model_dump(),
            "viewer_impression": settings.viewer_impression.model_dump(),
            "cors": settings.cors.model_dump(),
            "sc": settings.sc.model_dump(),
            "emotes": settings.emotes.model_dump(),
            "admin": settings.admin.model_dump(),
            "rate_limit": settings.rate_limit.model_dump(),
            "custom": self._config_cache or {}
        })

    @classmethod
    def _redact_secrets(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """把凭据换成 REDACTED 哨兵；空值保持空，以区分「没配」和「配了但不给看」。"""
        if isinstance(payload.get("ai"), dict):
            ai = payload["ai"]
            if cls._has_value(ai.get("api_key")):
                ai["api_key"] = cls.REDACTED
            providers = ai.get("providers")
            if isinstance(providers, list):
                for provider in providers:
                    if isinstance(provider, dict) and cls._has_value(
                        provider.get("api_key")
                    ):
                        provider["api_key"] = cls.REDACTED
        if isinstance(payload.get("admin"), dict) and cls._has_value(
            payload["admin"].get("api_key")
        ):
            payload["admin"]["api_key"] = cls.REDACTED
        if isinstance(payload.get("custom"), dict):
            payload["custom"] = cls._redact_mapping(payload["custom"])
        return payload

    @staticmethod
    def _has_value(value: Any) -> bool:
        """判断凭据是否真的配了。

        `SecretStr` 没有 `__bool__`，空密钥也是真值，直接判真会把「没配」
        显示成「配了但不给看」，所以先取出内部值再看。
        """
        if isinstance(value, SecretStr):
            return bool(value.get_secret_value())
        return bool(value)

    @classmethod
    def _redact_mapping(cls, value: Any) -> Any:
        """递归脱敏 custom 段：键名带 api_key/token/secret/password 的一律遮蔽。"""
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for key, item in value.items():
                lowered = str(key).casefold()
                if any(marker in lowered for marker in cls.SECRET_KEY_MARKERS):
                    out[key] = cls.REDACTED if cls._has_value(item) else item
                else:
                    out[key] = cls._redact_mapping(item)
            return out
        if isinstance(value, list):
            return [cls._redact_mapping(item) for item in value]
        return value


config_manager = ConfigManager()
