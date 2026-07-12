"""数据库、网络、安全和并发基础设施边界。"""

__all__ = [
    "AuthService", "BoundedWorkGate", "ConcurrencyGate", "DatabaseManager",
    "EventBus", "HttpProtectionMiddleware", "InMemoryRateLimiter",
    "OverloadProtector", "SecurityMetrics", "WebSocketRateGuard",
]


def __getattr__(name):
    modules = {
        "AuthService": (".auth", "AuthService"),
        "BoundedWorkGate": (".bounded_work_gate", "BoundedWorkGate"),
        "ConcurrencyGate": (".rate_limiter", "ConcurrencyGate"),
        "DatabaseManager": (".database", "DatabaseManager"),
        "EventBus": (".event_bus", "EventBus"),
        "HttpProtectionMiddleware": (".http_protection", "HttpProtectionMiddleware"),
        "InMemoryRateLimiter": (".rate_limiter", "InMemoryRateLimiter"),
        "OverloadProtector": (".overload_protection", "OverloadProtector"),
        "SecurityMetrics": (".security_metrics", "SecurityMetrics"),
        "WebSocketRateGuard": (".realtime_protection", "WebSocketRateGuard"),
    }
    if name not in modules:
        raise AttributeError(name)
    from importlib import import_module
    module_name, attribute = modules[name]
    return getattr(import_module(module_name, __name__), attribute)
