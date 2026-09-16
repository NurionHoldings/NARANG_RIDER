"""Uniform resource budgets for untrusted structured input.

Limits are based on payload shape, never user, rider, merchant, or branch identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class ResourceLimitExceeded(ValueError):
    pass


@dataclass(frozen=True)
class JsonBudget:
    max_bytes: int = 65_536
    max_depth: int = 12
    max_nodes: int = 512
    max_collection_items: int = 128
    max_string_chars: int = 4_096


DEFAULT_JSON_BUDGET = JsonBudget()


def bounded_json_object(data: bytes, budget: JsonBudget = DEFAULT_JSON_BUDGET) -> dict[str, Any]:
    if len(data) > budget.max_bytes:
        raise ResourceLimitExceeded("JSON_BYTES_EXCEEDED")
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as error:
        raise ResourceLimitExceeded("INVALID_JSON") from error
    if not isinstance(value, dict):
        raise ResourceLimitExceeded("JSON_OBJECT_REQUIRED")

    nodes = 0
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > budget.max_nodes or depth > budget.max_depth:
            raise ResourceLimitExceeded("JSON_SHAPE_EXCEEDED")
        if isinstance(item, dict):
            if len(item) > budget.max_collection_items:
                raise ResourceLimitExceeded("JSON_COLLECTION_EXCEEDED")
            for key, nested in item.items():
                if len(key) > budget.max_string_chars:
                    raise ResourceLimitExceeded("JSON_STRING_EXCEEDED")
                pending.append((nested, depth + 1))
        elif isinstance(item, list):
            if len(item) > budget.max_collection_items:
                raise ResourceLimitExceeded("JSON_COLLECTION_EXCEEDED")
            pending.extend((nested, depth + 1) for nested in item)
        elif isinstance(item, str) and len(item) > budget.max_string_chars:
            raise ResourceLimitExceeded("JSON_STRING_EXCEEDED")
    return value
