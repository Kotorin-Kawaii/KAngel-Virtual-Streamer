"""Audience 稳定公共 API。"""

from .domain import (
    AudienceRelationship,
    EmoteConfigResponse,
    EmoteDecision,
    ViewerIdentity,
    ViewerIdentityType,
)

__all__ = [
    "AudienceRelationship",
    "AudienceRelationshipManager",
    "EmoteConfigResponse",
    "EmoteDecision",
    "NicknameHistoryContextManager",
    "VerifiedAccountPrincipal",
    "ViewerEmoteService",
    "ViewerIdentity",
    "ViewerIdentityResolver",
    "ViewerIdentityType",
    "ViewerPresenceCoordinator",
]


def __getattr__(name: str):
    """延迟加载应用服务，避免领域模型导入触发数据库与全局服务初始化。"""
    if name in {
        "AudienceRelationshipManager", "NicknameHistoryContextManager",
        "VerifiedAccountPrincipal", "ViewerEmoteService",
        "ViewerIdentityResolver", "ViewerPresenceCoordinator",
    }:
        from . import application
        return getattr(application, name)
    raise AttributeError(name)
