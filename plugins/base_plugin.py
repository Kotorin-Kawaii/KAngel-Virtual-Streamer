from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from utils.logger import logger


class BasePlugin(ABC):
    """插件基类"""
    
    name: str = "base_plugin"
    version: str = "1.0.0"
    description: str = "基础插件"
    
    def __init__(self):
        self._enabled = False
        self._config: Dict[str, Any] = {}
    
    @abstractmethod
    async def on_load(self) -> None:
        """插件加载时调用"""
        pass
    
    @abstractmethod
    async def on_unload(self) -> None:
        """插件卸载时调用"""
        pass
    
    async def on_enable(self) -> None:
        """插件启用时调用"""
        self._enabled = True
        logger.info(f"插件已启用: {self.name} v{self.version}")
    
    async def on_disable(self) -> None:
        """插件禁用时调用"""
        self._enabled = False
        logger.info(f"插件已禁用: {self.name}")
    
    def is_enabled(self) -> bool:
        """检查插件是否启用"""
        return self._enabled
    
    def get_config(self, key: str, default: Any = None) -> Any:
        """获取插件配置"""
        return self._config.get(key, default)
    
    def set_config(self, key: str, value: Any) -> None:
        """设置插件配置"""
        self._config[key] = value
    
    async def on_danmaku_received(self, danmaku: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        收到弹幕时调用
        
        Args:
            danmaku: 弹幕数据
            
        Returns:
            修改后的弹幕数据，或None表示不修改
        """
        return None
    
    async def on_danmaku_broadcast(self, danmaku: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        弹幕广播前调用
        
        Args:
            danmaku: 弹幕数据
            
        Returns:
            修改后的弹幕数据，或None表示不修改
        """
        return None
    
    async def on_ai_reply_generated(self, reply: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        AI回复生成后调用
        
        Args:
            reply: AI回复数据
            
        Returns:
            修改后的回复数据，或None表示不修改
        """
        return None
