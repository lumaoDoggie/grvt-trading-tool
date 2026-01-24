from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from grvt_volume_boost.auth.cookies import (
    DEFAULT_COOKIE_REFRESH_INTERVAL_SEC,
    get_fresh_cookie,
    load_cookie_cache,
    save_cookie_cache,
)
from grvt_volume_boost.settings import SESSION_DIR


def _default_state_file(account: int) -> Path:
    return SESSION_DIR / f"grvt_browser_state_{account}.json"


def _mask_secret(value: str, *, prefix: int = 12) -> str:
    if not value:
        return ""
    if len(value) <= prefix:
        return value
    return value[:prefix] + "..."


def serve_cookies(*, state_file: Path, refresh_interval_sec: int) -> None:
    """Run as a service, refreshing cookies periodically."""
    print("=" * 60)
    print("GRVT Cookie Refresh Service")
    print("=" * 60)
    print(f"State file: {state_file}")
    print(f"Refresh interval: {refresh_interval_sec} seconds")
    print("=" * 60)

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            gravity = get_fresh_cookie(state_file)
            if gravity:
                save_cookie_cache(gravity)
                print(f"[{now}] Cookie refreshed: {gravity[:12]}...")
            else:
                print(f"[{now}] Failed to get cookie - manual login required")
                break
        except Exception as e:
            print(f"[{now}] Error: {e}")

        print(f"[{now}] Sleeping for {refresh_interval_sec} seconds...")
        time.sleep(refresh_interval_sec)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="GRVT Cookie Provider")
    parser.add_argument("--qr-login", type=str, metavar="IMAGE", help="Login via QR code image (PNG/JPG)")
    parser.add_argument("--headless", action="store_true", help="Run QR login in headless mode")
    parser.add_argument("--get-cookie", action="store_true", help="Get fresh cookie (headless)")
    parser.add_argument("--serve", action="store_true", help="Run as refresh service")
    parser.add_argument("--cached", action="store_true", help="Print cached cookie if still fresh (exit 1 if not)")
    parser.add_argument(
        "--print-secrets",
        action="store_true",
        help="Print full cookie value to stdout (unsafe; prefer the default masked output)",
    )
    parser.add_argument("--account", type=int, choices=[1, 2], default=1, help="Account state file selector (default: 1)")
    parser.add_argument("--state-file", type=str, help="Override state file path")
    parser.add_argument(
        "--refresh-interval",
        type=int,
        default=DEFAULT_COOKIE_REFRESH_INTERVAL_SEC,
        help="Cookie refresh interval seconds (default: 900)",
    )

    args = parser.parse_args(argv)

    state_file = Path(args.state_file) if args.state_file else _default_state_file(args.account)

    if args.qr_login:
        from grvt_volume_boost.auth.qr_login import qr_login
        qr_login(args.qr_login, state_file, headless=args.headless)
        return

    if args.get_cookie:
        cookie = get_fresh_cookie(state_file)
        if cookie:
            save_cookie_cache(cookie)
            if args.print_secrets:
                print(f"\nGRAVITY_COOKIE={cookie}")
            else:
                print(f"Cookie refreshed and cached: {_mask_secret(cookie)} (use --print-secrets to print full value)")
        else:
            sys.exit(1)
        return

    if args.serve:
        serve_cookies(state_file=state_file, refresh_interval_sec=args.refresh_interval)
        return

    if args.cached:
        cookie = load_cookie_cache()
        if cookie:
            if args.print_secrets:
                print(cookie)
            else:
                print(_mask_secret(cookie))
            return
        sys.exit(1)

    parser.print_help()


if __name__ == "__main__":
    main()
