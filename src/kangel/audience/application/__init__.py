"""Audience 应用服务公共入口（延迟加载）。"""

_MODULES = {
    "AudienceRelationshipManager": "relationship_service",
    "NicknameHistoryContextManager": "nickname_history",
    "VerifiedAccountPrincipal": "identity_service",
    "ViewerEmoteService": "emote_service",
    "ViewerIdentityResolver": "identity_service",
    "ViewerPresenceCoordinator": "presence_service",
}
__all__ = list(_MODULES)

def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
