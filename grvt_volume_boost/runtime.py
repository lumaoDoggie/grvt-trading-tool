from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    # PyInstaller sets sys.frozen and sys._MEIPASS for onefile. For onedir builds,
    # sys.frozen is still True.
    return bool(getattr(sys, "frozen", False))


def _exe_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    # Dev mode: repo root is one level above package.
    return Path(__file__).resolve().parent.parent


def ensure_playwright_browsers_path() -> None:
    """Point Playwright to a bundled browsers folder when running as an EXE.

    For a true "one-click" Windows experience we ship a Chromium build next to the EXE
    under `playwright-browsers/`. When this folder exists, set
    PLAYWRIGHT_BROWSERS_PATH so Playwright can find it without downloading.
    """
    # Respect an explicit user override.
    if os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
        return

    base = _exe_dir()
    bundled = base / "playwright-browsers"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)

