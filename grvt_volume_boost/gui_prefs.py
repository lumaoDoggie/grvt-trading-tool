from __future__ import annotations

import json
import os
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PREFS_FILE = _REPO_ROOT / "grvt_gui_prefs.json"


def load_prefs() -> dict:
    try:
        if not _PREFS_FILE.exists():
            return {}
        with open(_PREFS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(prefs: dict) -> None:
    try:
        with open(_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass


def save_env(env: str) -> None:
    env = (env or "").strip().lower()
    if env not in ("prod", "testnet"):
        return
    prefs = load_prefs()
    prefs["env"] = env
    save_prefs(prefs)


def save_lang(lang: str) -> None:
    lang = (lang or "").strip().lower()
    if lang not in ("en", "zh"):
        return
    prefs = load_prefs()
    prefs["lang"] = lang
    save_prefs(prefs)


def apply_startup_prefs() -> None:
    """Apply persisted GUI prefs before importing settings-heavy modules.

    If GRVT_ENV is already set (e.g., via .env or the shell), we keep it.
    """
    prefs = load_prefs()
    if not os.getenv("GRVT_ENV"):
        env = (prefs.get("env") or "").strip().lower()
        if env in ("prod", "testnet"):
            os.environ["GRVT_ENV"] = env
    if not os.getenv("GRVT_LANG"):
        lang = (prefs.get("lang") or "").strip().lower()
        if lang in ("en", "zh"):
            os.environ["GRVT_LANG"] = lang
