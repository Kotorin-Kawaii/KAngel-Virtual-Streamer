"""P25 自愿赞助集成（延迟加载）。

赞助只影响展示，不授予任何功能权益。
"""

_MODULES = {
    "AfdianClient": "client",
    "AfdianError": "client",
    "SponsorService": "service",
    "SponsorSyncWorker": "sync_worker",
    "afdian_client": "client",
    "sponsor_service": "service",
    "sponsor_sync_worker": "sync_worker",
}
__all__ = list(_MODULES)


def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
