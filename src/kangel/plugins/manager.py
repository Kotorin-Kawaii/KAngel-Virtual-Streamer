import importlib
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from .base import BasePlugin
from .context import PluginContext
from config import settings
from kangel.shared.logging import logger


class PluginManager:
    """插件管理器"""

    def __init__(self, context: Optional[PluginContext] = None):
        self._plugins: Dict[str, BasePlugin] = {}
        self._plugin_dir = Path(settings.plugins.plugin_dir)
        self._loaded = False
        self._context = context or PluginContext()

    async def load_plugins(self) -> None:
        """加载所有插件"""
        if self._loaded:
            return

        logger.info("开始加载插件...")

        if not self._plugin_dir.exists():
            logger.warning(f"插件目录不存在: {self._plugin_dir}")
            return

        enabled_plugins = set(settings.plugins.enabled_plugins)

        for item in self._plugin_dir.iterdir():
            if item.is_dir() and (item / "__init__.py").exists():
                plugin_name = item.name
                await self._load_plugin(plugin_name, plugin_name in enabled_plugins)

        self._loaded = True
        logger.info(f"插件加载完成，共加载 {len(self._plugins)} 个插件")

    async def _load_plugin(self, plugin_name: str, enable: bool = False) -> Optional[BasePlugin]:
        """加载单个插件"""
        try:
            module_path = f"plugins.{plugin_name}"
            module = importlib.import_module(module_path)

            if hasattr(module, "plugin"):
                plugin = module.plugin
            elif hasattr(module, "Plugin"):
                plugin_class = module.Plugin
                plugin = plugin_class()
            else:
                logger.warning(f"插件 {plugin_name} 未找到插件类")
                return None

            if not isinstance(plugin, BasePlugin):
                logger.warning(f"插件 {plugin_name} 不是BasePlugin的子类")
                return None

            plugin.bind_context(self._context)

            await plugin.on_load()
            self._plugins[plugin.name] = plugin

            if enable:
                await plugin.on_enable()

            logger.info(f"插件加载成功: {plugin.name} v{plugin.version}")
            return plugin

        except Exception as e:
            logger.error(f"加载插件 {plugin_name} 失败: {e}")
            return None

    async def unload_plugin(self, plugin_name: str) -> bool:
        """卸载插件"""
        if plugin_name not in self._plugins:
            return False

        plugin = self._plugins[plugin_name]
        if plugin.is_enabled():
            await plugin.on_disable()

        await plugin.on_unload()
        del self._plugins[plugin_name]
        logger.info(f"插件已卸载: {plugin_name}")
        return True

    async def enable_plugin(self, plugin_name: str) -> bool:
        """启用插件"""
        plugin = self._plugins.get(plugin_name)
        if not plugin:
            return False

        if plugin.is_enabled():
            return True

        await plugin.on_enable()
        return True

    async def disable_plugin(self, plugin_name: str) -> bool:
        """禁用插件"""
        plugin = self._plugins.get(plugin_name)
        if not plugin:
            return False

        if not plugin.is_enabled():
            return True

        await plugin.on_disable()
        return True

    def get_plugin(self, plugin_name: str) -> Optional[BasePlugin]:
        """获取插件"""
        return self._plugins.get(plugin_name)

    def list_plugins(self) -> List[Dict[str, Any]]:
        """列出所有插件"""
        return [
            {
                "name": plugin.name,
                "version": plugin.version,
                "description": plugin.description,
                "enabled": plugin.is_enabled()
            }
            for plugin in self._plugins.values()
        ]

    async def emit_event(self, event_name: str, *args, **kwargs) -> List[Any]:
        """
        向所有启用的插件发送事件

        Returns:
            所有非None的返回值列表
        """
        results = []
        for plugin in self._plugins.values():
            if not plugin.is_enabled():
                continue

            handler_name = f"on_{event_name}"
            handler = getattr(plugin, handler_name, None)
            if handler and callable(handler):
                try:
                    result = await handler(*args, **kwargs)
                    if result is not None:
                        results.append(result)
                except Exception as e:
                    logger.error(f"插件 {plugin.name} 处理事件 {event_name} 失败: {e}")

        return results


plugin_manager = PluginManager()
