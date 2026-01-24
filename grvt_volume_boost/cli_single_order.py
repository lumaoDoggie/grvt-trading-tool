from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from grvt_volume_boost.auth.cookies import load_cookie_cache
from grvt_volume_boost.auth.qr_login import decode_qr_image, qr_login_from_url
from grvt_volume_boost.clients.market_data import get_instrument
from grvt_volume_boost.config import get_account
from grvt_volume_boost.services.orders import place_market_order
from grvt_volume_boost.sizing import normalize_size
from grvt_volume_boost.settings import SESSION_DIR


def main(argv: list[str] | None = None) -> int:
    load_dotenv(".env")

    p = argparse.ArgumentParser(description="Place a single GRVT order (optionally via QR login).")
    p.add_argument("--account", type=int, choices=[1, 2], default=1, help="Account number (default: 1)")
    p.add_argument("--market", type=str, default="BTC_USDT_Perp", help="Instrument (default: BTC_USDT_Perp)")
    p.add_argument("--side", type=str, choices=["buy", "sell"], required=True, help="Order side")
    p.add_argument("--size", type=str, required=True, help="Base size (e.g. 0.002 for BTC)")
    p.add_argument("--qr-image", type=str, help="Path to a fresh QR image (PNG/JPG). If set, performs login first.")
    p.add_argument(
        "--qr-watch",
        type=float,
        default=0.0,
        help="If >0, keep watching --qr-image for a fresh QR (seconds) until login succeeds.",
    )
    p.add_argument("--wait-session-key", type=float, default=600.0, help="Seconds to wait for email verification")
    p.add_argument("--confirm", action="store_true", help="Actually place the order (required)")
    args = p.parse_args(argv)

    SESSION_DIR.mkdir(exist_ok=True)
    state_path = SESSION_DIR / f"grvt_browser_state_{args.account}.json"

    cookie: str | None = None
    if args.qr_image:
        deadline = time.time() + float(args.qr_watch or 0.0)
        last_url: str | None = None
        while True:
            url = decode_qr_image(args.qr_image)
            if not url:
                if args.qr_watch and time.time() < deadline:
                    time.sleep(1.0)
                    continue
                print("Failed to decode QR image.", file=sys.stderr)
                return 2

            if url != last_url:
                last_url = url
                cookie, msg = qr_login_from_url(
                    url,
                    state_path,
                    headless=False,
                    require_session_key=True,
                    session_key_timeout_sec=float(args.wait_session_key),
                )
                print(msg)
                if cookie:
                    break

            if not args.qr_watch or time.time() >= deadline:
                return 1
            time.sleep(1.0)

    # Load account (needs grvt_ss_on_chain in the saved browser state).
    try:
        acc = get_account(args.account)
    except Exception as e:
        print(f"Failed to load account {args.account}: {e}", file=sys.stderr)
        return 2

    inst_info = get_instrument(args.market)
    size = normalize_size(inst_info, Decimal(args.size)).size
    is_buying = args.side == "buy"

    # Prefer the cookie returned from QR login; fall back to cache.
    cookie = cookie or load_cookie_cache(max_age_sec=60 * 60)
    if not cookie:
        print("No gravity cookie available. Re-run with --qr-image to login.", file=sys.stderr)
        return 2

    if not args.confirm:
        print(
            f"Dry run: would place MARKET {args.side.upper()} {size} {args.market} on Account {args.account}.\n"
            "Re-run with --confirm to place the live order.",
            file=sys.stderr,
        )
        return 2

    result = place_market_order(acc, cookie, args.market, inst_info, size, is_buying=is_buying)
    print(json.dumps(result, indent=2))
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
