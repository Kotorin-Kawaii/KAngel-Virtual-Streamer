"""Stream 稳定公共 API。"""

from .domain import (
    DailyStreamPlanBeat,
    DailyStreamPlanSnapshot,
    ScheduleSnapshot,
    StreamMainlineState,
    StreamScheduleService,
)

_APPLICATION = {
    "DailyStreamPlanService", "DailyThemeService", "DailyThemeSnapshot",
    "DeterministicStreamDirector", "DirectorSignalTracker", "MetadataEventType",
    "MoodPusher", "SessionSummaryValidator", "StreamDirectorRuntime",
    "StreamMainlineService", "StreamMetadata", "StreamMetadataPusher",
    "StreamerActionDecision", "StreamerActionExecutor", "StreamSessionSummaryConsumer",
    "StreamSessionSummaryService", "StreamerActivityService", "StreamerActivityState",
    "UserActivity",
}
__all__ = [
    "DailyStreamPlanBeat", "DailyStreamPlanSnapshot", "ScheduleSnapshot",
    "StreamMainlineState", "StreamScheduleService", *_APPLICATION,
]

def __getattr__(name: str):
    if name in _APPLICATION:
        from . import application
        return getattr(application, name)
    raise AttributeError(name)
