"""Stream 应用服务公共入口（延迟加载）。"""

_MODULES = {
    "DailyThemeService": "daily_theme",
    "DailyThemeSnapshot": "daily_theme",
    "MetadataEventType": "metadata",
    "MoodPusher": "mood_pusher",
    "StreamMetadata": "metadata",
    "StreamMetadataPusher": "metadata",
    "StreamerActivityService": "activity",
    "StreamerActivityState": "activity",
    "UserActivity": "metadata",
}
__all__ = list(_MODULES)

def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
