from __future__ import annotations

from typing import Any


def deep_contains(obj: Any, needle: str) -> bool:
    """Best-effort recursive search for `needle` in dict/list/strings.

    Used for matching GRVT WS payloads which can vary by environment/version.
    """
    if obj is None:
        return False
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, (int, float, bool)):
        return False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and needle in k:
                return True
            if deep_contains(v, needle):
                return True
        return False
    if isinstance(obj, list):
        return any(deep_contains(v, needle) for v in obj)
    return False

