import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
from .settings import Settings, settings


class ConfigManager:
    """配置管理器，支持多配置源"""
    
    def __init__(self, config_file: Optional[str] = None):
        self._config_file = config_file or "config.json"
        self._config_cache: Optional[Dict[str, Any]] = None
        self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        # 延迟导入logger避免循环导入
        from utils.logger import logger
        
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
        from utils.logger import logger
        
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
        if self._config_cache is None:
            self._config_cache = {}
        self._config_cache[key] = value
        self.save_config()
    
    def update_settings(self, settings_obj: Settings) -> Settings:
        """从配置文件更新Settings对象"""
        from utils.logger import logger
        
        if not self._config_cache:
            return settings_obj
        
        try:
            if "server" in self._config_cache:
                for key, value in self._config_cache["server"].items():
                    if hasattr(settings_obj.server, key):
                        setattr(settings_obj.server, key, value)
            
            if "ai" in self._config_cache:
                for key, value in self._config_cache["ai"].items():
                    if hasattr(settings_obj.ai, key):
                        setattr(settings_obj.ai, key, value)
            
            if "danmaku" in self._config_cache:
                for key, value in self._config_cache["danmaku"].items():
                    if hasattr(settings_obj.danmaku, key):
                        setattr(settings_obj.danmaku, key, value)
            
            if "persona" in self._config_cache:
                for key, value in self._config_cache["persona"].items():
                    if hasattr(settings_obj.persona, key):
                        setattr(settings_obj.persona, key, value)

            if "stream" in self._config_cache:
                for key, value in self._config_cache["stream"].items():
                    if hasattr(settings_obj.stream, key):
                        setattr(settings_obj.stream, key, value)

            if "memory" in self._config_cache:
                for key, value in self._config_cache["memory"].items():
                    if hasattr(settings_obj.memory, key):
                        setattr(settings_obj.memory, key, value)

            if "cors" in self._config_cache:
                for key, value in self._config_cache["cors"].items():
                    if hasattr(settings_obj.cors, key):
                        setattr(settings_obj.cors, key, value)

            if "sc" in self._config_cache:
                for key, value in self._config_cache["sc"].items():
                    if hasattr(settings_obj.sc, key):
                        setattr(settings_obj.sc, key, value)

            if "emotes" in self._config_cache:
                for key, value in self._config_cache["emotes"].items():
                    if hasattr(settings_obj.emotes, key):
                        setattr(settings_obj.emotes, key, value)

            if "admin" in self._config_cache:
                for key, value in self._config_cache["admin"].items():
                    if hasattr(settings_obj.admin, key):
                        setattr(settings_obj.admin, key, value)

            if "rate_limit" in self._config_cache:
                for key, value in self._config_cache["rate_limit"].items():
                    if hasattr(settings_obj.rate_limit, key):
                        setattr(settings_obj.rate_limit, key, value)
            
            logger.info("Settings从配置文件更新成功")
        except Exception as e:
            logger.error(f"更新Settings失败: {e}")
        
        return settings_obj
    
    def export_config(self) -> Dict[str, Any]:
        """导出完整配置"""
        return {
            "server": settings.server.model_dump(),
            "ai": settings.ai.model_dump(),
            "danmaku": settings.danmaku.model_dump(),
            "persona": settings.persona.model_dump(),
            "plugins": settings.plugins.model_dump(),
            "stream": settings.stream.model_dump(),
            "memory": settings.memory.model_dump(),
            "cors": settings.cors.model_dump(),
            "sc": settings.sc.model_dump(),
            "emotes": settings.emotes.model_dump(),
            "admin": settings.admin.model_dump(),
            "rate_limit": settings.rate_limit.model_dump(),
            "custom": self._config_cache or {}
        }


config_manager = ConfigManager()
