"""Stream 稳定公共 API。"""

from .domain import ScheduleSnapshot, StreamScheduleService

_APPLICATION = {"DailyThemeService", "DailyThemeSnapshot", "MetadataEventType", "MoodPusher", "StreamMetadata", "StreamMetadataPusher", "StreamerActivityService", "StreamerActivityState", "UserActivity"}
__all__ = ["ScheduleSnapshot", "StreamScheduleService", *_APPLICATION]

def __getattr__(name: str):
    if name in _APPLICATION:
        from . import application
        return getattr(application, name)
    raise AttributeError(name)
