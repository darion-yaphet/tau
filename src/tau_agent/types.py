"""Shared low-level types for Tau's portable agent layer."""

# Tau 可移植智能体层共享的底层类型。

from __future__ import annotations

# Pydantic needs PEP 695 named recursive aliases for JSON-like values.

# Pydantic 需要使用 PEP 695 命名递归别名来表示类 JSON 值。
type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]
