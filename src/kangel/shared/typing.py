"""跨层 JSON 类型别名。"""

from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

__all__ = ["JsonScalar", "JsonValue", "JsonObject"]
