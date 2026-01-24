from __future__ import annotations

import os
import sys
import traceback
from typing import Any


def debug_enabled() -> bool:
    raw = (os.getenv("GRVT_DEBUG") or "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def debug(msg: str, *, exc: BaseException | None = None, extra: dict[str, Any] | None = None) -> None:
    """Write debug logs to stderr when GRVT_DEBUG is enabled."""
    if not debug_enabled():
        return
    try:
        if extra:
            msg = msg + " " + " ".join(f"{k}={v}" for k, v in extra.items())
        print(f"[DEBUG] {msg}", file=sys.stderr)
        if exc is not None:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    except Exception:
        pass

