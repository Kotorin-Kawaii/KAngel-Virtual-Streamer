"""Stream 应用服务公共入口（延迟加载）。"""

_MODULES = {
    "DailyThemeService": "daily_theme",
    "DailyThemeSnapshot": "daily_theme",
    "DailyStreamPlanService": "mainline",
    "StreamMainlineService": "mainline",
    "DeterministicStreamDirector": "director",
    "DirectorSignalTracker": "director",
    "StreamDirectorRuntime": "director",
    "StreamerActionDecision": "director",
    "StreamerActionExecutor": "director",
    "IdleState": "idle_state",
    "IdleStateResolver": "idle_state",
    "MetadataEventType": "metadata",
    "MoodPusher": "mood_pusher",
    "StreamMetadata": "metadata",
    "StreamMetadataPusher": "metadata",
    "StreamerActivityService": "activity",
    "StreamerActivityState": "activity",
    "StreamerBeat": "beat",
    "StreamerBeatScheduler": "beat",
    "SessionSummaryValidator": "session_summary",
    "StreamSessionSummaryService": "session_summary",
    "StreamSessionSummaryConsumer": "session_summary",
    "UserActivity": "metadata",
}
__all__ = list(_MODULES)

def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
