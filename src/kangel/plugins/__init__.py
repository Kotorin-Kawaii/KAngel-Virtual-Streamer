"""插件稳定公共边界。"""

from .base import BasePlugin
from .context import PluginContext
from .manager import PluginManager, plugin_manager

__all__ = ["BasePlugin", "PluginContext", "PluginManager", "plugin_manager"]
