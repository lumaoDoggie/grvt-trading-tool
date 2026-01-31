from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from grvt_volume_boost.playwright_compat import run_sync_playwright
from grvt_volume_boost.runtime import ensure_playwright_browsers_path
from grvt_volume_boost.settings import COOKIE_CACHE_FILE, ORIGIN
from grvt_volume_boost.i18n import tr

# Cookie expires in ~25 minutes; refresh service runs every 15 minutes by default.
DEFAULT_COOKIE_REFRESH_INTERVAL_SEC = 15 * 60


def save_cookie_cache(gravity: str, *, cache_file: Path = COOKIE_CACHE_FILE) -> None:
    cache = {
        "gravity": gravity,
        "timestamp": time.time(),
        "datetime": datetime.now().isoformat(),
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def load_cookie_cache(
    *,
    cache_file: Path = COOKIE_CACHE_FILE,
    max_age_sec: float = DEFAULT_COOKIE_REFRESH_INTERVAL_SEC,
) -> str | None:
    if not cache_file.exists():
        return None

    with open(cache_file, "r", encoding="utf-8") as f:
        cache = json.load(f)

    age = time.time() - float(cache.get("timestamp", 0))
    if age > max_age_sec:
        return None

    gravity = cache.get("gravity")
    return str(gravity) if gravity else None


def _ensure_playwright_format(state_path: Path, origin: str) -> dict:
    """Load state file and convert to Playwright format if needed."""
    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    # Already Playwright format
    if "origins" in state:
        return state

    # Raw localStorage format - convert
    return {
        "cookies": [],
        "origins": [{"origin": origin, "localStorage": [{"name": k, "value": v} for k, v in state.items()]}]
    }


def validate_state_file(state_path: Path) -> tuple[bool, str]:
    """Quick validation of browser state file without launching browser.

    Returns (is_valid, error_message). If is_valid is True, error_message is empty.
    """
    return validate_state_file_ext(state_path)


def validate_state_file_ext(
    state_path: Path,
    *,
    require_session_key: bool = False,
    origin: str = ORIGIN,
) -> tuple[bool, str]:
    """Extended validation for a browser state file.

    - Validates Playwright storage-state format and cookies.
    - Optionally validates presence of `localStorage['grvt_ss_on_chain']` which is required for trading.
    """
    if not state_path.exists():
        return False, tr("session.missing", name=state_path.name)

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except json.JSONDecodeError:
        return False, tr("session.invalid_json", name=state_path.name)

    if "origins" not in state:
        return False, tr("session.raw_localstorage", name=state_path.name)

    # Check if there are any cookies
    cookies = state.get("cookies", [])
    if not cookies:
        return False, tr("session.no_cookies", name=state_path.name)

    if require_session_key:
        # Prefer matching origin, but fall back to scanning all origins.
        origins = state.get("origins", []) or []
        entries = []
        for entry in origins:
            if entry.get("origin") == origin:
                entries.append(entry)
        if not entries:
            entries = origins

        found = False
        for entry in entries:
            for item in entry.get("localStorage", []) or []:
                if item.get("name") == "grvt_ss_on_chain" and item.get("value"):
                    found = True
                    break
            if found:
                break
        if not found:
            return False, tr("session.missing_session_key", name=state_path.name)

    return True, ""


def get_fresh_cookie(state_path: Path, *, origin: str = ORIGIN, force_refresh: bool = False) -> str | None:
    """Get fresh gravity cookie from stored browser session.

    The state file must be a full Playwright state (with cookies) from a logged-in session.
    Raw localStorage exports won't work - use QR login to create proper state.
    """
    if not state_path.exists():
        return None

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    # Check if this is raw localStorage (won't work for cookie refresh)
    if "origins" not in state:
        return None

    if not force_refresh:
        # Fast path: storage-state already contains a (usually valid) gravity cookie.
        # Avoid launching a browser on every run; it's slow and often unnecessary.
        try:
            now = time.time()
            for c in state.get("cookies", []) or []:
                if c.get("name") != "gravity":
                    continue
                val = c.get("value")
                if not val:
                    continue
                exp = c.get("expires")
                # If expiry is present and not expired, trust it.
                if exp is None:
                    return str(val)
                try:
                    exp_f = float(exp)
                except Exception:
                    return str(val)
                if exp_f <= 0 or exp_f > now:
                    return str(val)
        except Exception:
            pass

    gravity = None
    try:
        def _run() -> str | None:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth

            ensure_playwright_browsers_path()
            with sync_playwright() as p:
                stealth = Stealth()
                stealth.hook_playwright_context(p)
                browser = p.chromium.launch(
                    headless=True, args=["--headless=new", "--disable-blink-features=AutomationControlled"]
                )
                context = browser.new_context(
                    storage_state=state,
                    viewport={"width": 412, "height": 915},
                    device_scale_factor=2.625,
                    is_mobile=True,
                    has_touch=True,
                    user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
                    locale="en-US",
                )
                page = context.new_page()

                # `networkidle` can be slow/flaky due to analytics/streams; `domcontentloaded` is enough.
                page.goto(origin, wait_until="domcontentloaded", timeout=60000)

                # Wait for cookie to appear
                start = time.time()
                g = None
                while time.time() - start < 15.0:
                    cookies = context.cookies()
                    g = next((c["value"] for c in cookies if c.get("name") == "gravity"), None)
                    if g:
                        break
                    page.wait_for_timeout(500)

                if g:
                    context.storage_state(path=str(state_path))
                browser.close()
                return g

        gravity = run_sync_playwright(_run)
    except Exception as e:
        import traceback
        print(f"[Cookie Refresh] ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        return None

    return gravity


def get_cookies_parallel(state_path_a: Path, state_path_b: Path) -> tuple[str | None, str | None]:
    """Fetch cookies for two state files in parallel."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(get_fresh_cookie, state_path_a)
        future_b = executor.submit(get_fresh_cookie, state_path_b)
        return future_a.result(), future_b.result()
