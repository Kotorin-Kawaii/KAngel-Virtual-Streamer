"""跨层通用错误类型。"""


class KangelError(Exception):
    """可预期的应用基础错误。"""


class ConfigurationError(KangelError):
    """配置无法安全解析。"""


class InvariantViolation(KangelError):
    """领域或应用不变量被破坏。"""


__all__ = ["KangelError", "ConfigurationError", "InvariantViolation"]
