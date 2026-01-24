from __future__ import annotations

import inspect
from typing import Any, Mapping

import websockets

# websockets renamed `extra_headers` -> `additional_headers` in newer releases.
# We support both to avoid runtime errors that only show up when connecting.
_CONNECT_PARAMS = set(inspect.signature(websockets.connect).parameters)
if "additional_headers" in _CONNECT_PARAMS:
    _HEADERS_KW = "additional_headers"
elif "extra_headers" in _CONNECT_PARAMS:
    _HEADERS_KW = "extra_headers"
else:
    _HEADERS_KW = None


def connect(uri: str, *, headers: Mapping[str, str] | None = None, **kwargs: Any):
    """Compatibility wrapper around websockets.connect for auth headers."""
    if headers is None or _HEADERS_KW is None:
        return websockets.connect(uri, **kwargs)
    return websockets.connect(uri, **{_HEADERS_KW: dict(headers)}, **kwargs)

