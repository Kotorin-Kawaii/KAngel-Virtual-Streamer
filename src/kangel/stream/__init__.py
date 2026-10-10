"""Stream 稳定公共 API。"""

from .domain import (
    ACTIVE_P32B_ACTIONS,
    LIVE_ACT_REASON_CODES,
    SPEECH_THREAD_SOURCES,
    DailyStreamPlanBeat,
    DailyStreamPlanSnapshot,
    ScheduleSnapshot,
    StreamMainlineState,
    StreamScheduleService,
    LiveActDecision,
    LiveActSnapshot,
    LiveActType,
    LivePresenceContext,
    LivePresenceState,
    SpeechThread,
    SpeechThreadStatus,
)

_APPLICATION = {
    "DailyStreamPlanService", "DailyThemeService", "DailyThemeSnapshot",
    "DeterministicStreamDirector", "DirectorSignalTracker", "MetadataEventType",
    "MoodPusher", "SessionSummaryValidator", "StreamDirectorRuntime",
    "StreamMainlineService", "StreamMetadata", "StreamMetadataPusher",
    "StreamerActionDecision", "StreamerActionExecutor", "StreamSessionSummaryConsumer",
    "StreamSessionSummaryService", "StreamerActivityService", "StreamerActivityState",
    "UserActivity", "DeterministicLivePresencePolicy", "LivePresenceMetrics",
    "LivePresenceService", "LivePresenceShadowObservation", "LivePresenceVersionConflict",
    "live_presence_service",
}
__all__ = [
    "ACTIVE_P32B_ACTIONS", "LIVE_ACT_REASON_CODES", "SPEECH_THREAD_SOURCES",
    "DailyStreamPlanBeat", "DailyStreamPlanSnapshot", "ScheduleSnapshot",
    "StreamMainlineState", "StreamScheduleService", "LiveActDecision",
    "LiveActSnapshot", "LiveActType", "LivePresenceContext", "LivePresenceState",
    "SpeechThread", "SpeechThreadStatus", *_APPLICATION,
]

def __getattr__(name: str):
    if name in _APPLICATION:
        from . import application
        return getattr(application, name)
    raise AttributeError(name)
