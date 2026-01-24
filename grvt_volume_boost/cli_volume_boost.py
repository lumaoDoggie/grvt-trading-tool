from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from datetime import datetime
from decimal import Decimal

import requests
from dotenv import load_dotenv

from grvt_volume_boost.auth.cookies import get_cookies_parallel
from grvt_volume_boost.config import get_all_accounts
from grvt_volume_boost.direction import (
    SIDE_POLICY_ACCOUNT1_LONG,
    SIDE_POLICY_ACCOUNT1_SHORT,
    SIDE_POLICY_RANDOM,
    AccountPair,
    choose_long_short_for_open,
)
from grvt_volume_boost.doctor import run_doctor
from grvt_volume_boost.services.orders import get_position_size
from grvt_volume_boost.sizing import compute_size_from_usd_notional, normalize_size
from grvt_volume_boost.settings import CHAIN_ID, TRADES_URL
from grvt_volume_boost.strategy import run_instant_round, run_normal_mode

_shutdown_requested = False
_active_accounts = []  # [(acc, cookie, instrument, inst_info), ...]
_log_file: str | None = None


def _log(level: str, msg: str) -> None:
    timestamp = datetime.now().isoformat()
    print(f"[{level}] {msg}")
    if _log_file:
        try:
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": timestamp, "level": level, "msg": msg}) + "\n")
        except Exception:
            pass


def _alert(msg: str, *, critical: bool = False) -> None:
    level = "CRITICAL" if critical else "WARN"
    _log(level, msg)
    if critical:
        print(f"\033[1;31m{'='*60}\n{level}: {msg}\n{'='*60}\033[0m")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if bot_token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"[{level}] {msg}"},
                timeout=5,
            )
        except Exception:
            pass


def _shutdown_handler(signum, frame) -> None:
    global _shutdown_requested
    if _shutdown_requested:
        print("\nForce quit!")
        sys.exit(1)
    _shutdown_requested = True
    print("\n\033[1;33mShutdown requested - cleaning up...\033[0m")


def main(argv: list[str] | None = None) -> None:
    global _active_accounts, _log_file

    load_dotenv(".env")

    if argv is None:
        argv = sys.argv[1:]

    if len(argv) >= 1 and argv[0] == "doctor":
        run_doctor()
        return

    parser = argparse.ArgumentParser(description="Volume Boost Trading")
    parser.add_argument("--market", default="CRV_USDT_Perp", help="Market to trade")
    parser.add_argument("--size", type=float, default=100, help="Size per order (contracts/base units)")
    parser.add_argument("--notional-usd", type=float, help="USD notional per order (overrides --size)")
    parser.add_argument(
        "--direction",
        choices=[SIDE_POLICY_RANDOM, SIDE_POLICY_ACCOUNT1_LONG, SIDE_POLICY_ACCOUNT1_SHORT],
        default=SIDE_POLICY_RANDOM,
        help="Direction policy for Account 1 in opening leg (modes 1-3)",
    )
    parser.add_argument("--rounds", type=int, default=10, help="Number of rounds")
    parser.add_argument("--instant-close", action="store_true", help="Instant close mode")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between open/close in instant mode")
    parser.add_argument("--max-margin", type=float, default=0.15, help="Max margin ratio (normal mode)")
    parser.add_argument("--hold", type=int, default=30, help="Hold time in minutes (normal mode)")
    parser.add_argument("--confirm", action="store_true", help="Confirm production trading")
    parser.add_argument("--log", type=str, help="JSONL log file path")
    args = parser.parse_args(argv)

    print("\033[1;31m" + "=" * 60)
    print("  ██████╗ ██████╗  ██████╗ ██████╗ ██╗   ██╗ ██████╗████████╗██╗ ██████╗ ███╗   ██╗")
    print("  ██╔══██╗██╔══██╗██╔═══██╗██╔══██╗██║   ██║██╔════╝╚══██╔══╝██║██╔═══██╗████╗  ██║")
    print("  ██████╔╝██████╔╝██║   ██║██║  ██║██║   ██║██║        ██║   ██║██║   ██║██╔██╗ ██║")
    print("  ██╔═══╝ ██╔══██╗██║   ██║██║  ██║██║   ██║██║        ██║   ██║██║   ██║██║╚██╗██║")
    print("  ██║     ██║  ██║╚██████╔╝██████╔╝╚██████╔╝╚██████╗   ██║   ██║╚██████╔╝██║ ╚████║")
    print("  ╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═════╝  ╚═════╝  ╚═════╝   ╚═╝   ╚═╝ ╚═════╝ ╚═╝  ╚═══╝")
    print("=" * 60 + "\033[0m")
    print(f"Chain ID: {CHAIN_ID} | Trades: {TRADES_URL}")

    if not args.confirm:
        print("\n\033[1;33mThis will execute REAL trades on PRODUCTION.\033[0m")
        print("Add --confirm flag to proceed, or run 'python volume_boost.py doctor' first.")
        return

    if args.log:
        _log_file = args.log
        _log("INFO", f"Logging to {_log_file}")

    signal.signal(signal.SIGINT, _shutdown_handler)

    accounts, errors = get_all_accounts()
    if errors:
        print("Config warnings:")
        for e in errors:
            print(f"  - {e}")
        print()

    if len(accounts) < 2:
        print("ERROR: Need 2 accounts logged in via QR (create session files for Account 1 and 2)")
        return

    # Preserve Account 1/2 ordering (session file #1/#2).
    pair = AccountPair(primary=accounts[0], secondary=accounts[1])
    print(f"\nAccount 1 (primary): {pair.primary.name} (sub_account_id={pair.primary.sub_account_id})")
    print(f"Account 2: {pair.secondary.name} (sub_account_id={pair.secondary.sub_account_id})")

    print("\nGetting cookies...")
    cookie_primary, cookie_secondary = get_cookies_parallel(
        pair.primary.browser_state_path, pair.secondary.browser_state_path
    )
    if not cookie_primary or not cookie_secondary:
        _alert("Failed to get cookies", critical=True)
        return
    print("Cookies OK\n")

    from grvt_volume_boost.clients.market_data import get_instrument

    inst_info = get_instrument(args.market)
    if args.notional_usd is not None:
        notional = Decimal(str(args.notional_usd))
        ticker = get_ticker(args.market)
        size, mid = compute_size_from_usd_notional(inst_info, ticker, notional)
        _log("INFO", f"Computed size from notional ${notional}: size={size} @ mid={mid}")
    else:
        size = Decimal(str(args.size))
        size = normalize_size(inst_info, size).size

    _active_accounts = [
        (pair.primary, cookie_primary, args.market, inst_info),
        (pair.secondary, cookie_secondary, args.market, inst_info),
    ]

    start_time = time.time()
    total_volume = Decimal(0)
    success_count = 0
    fail_count = 0

    if args.instant_close:
        print("=== INSTANT CLOSE MODE ===")
        print(f"Market: {args.market}")
        print(f"Size: {size}")
        print(f"Direction policy: {args.direction}")
        print(f"Rounds: {args.rounds}")
        print(f"Delay: {args.delay}s\n")

        for i in range(args.rounds):
            if _shutdown_requested:
                break
            print(f"[Round {i+1}/{args.rounds}]")

            long_acc, short_acc = choose_long_short_for_open(pair, args.direction)
            cookie_long = cookie_primary if long_acc is pair.primary else cookie_secondary
            cookie_short = cookie_secondary if short_acc is pair.secondary else cookie_primary

            success, mid_price, error_msg = run_instant_round(
                long_acc,
                short_acc,
                cookie_long,
                cookie_short,
                args.market,
                inst_info,
                size,
                delay=args.delay,
            )

            if error_msg:
                _alert(f"Round {i+1}: {error_msg}", critical=True)
                break

            if success:
                round_volume = 4 * size * mid_price
                total_volume += round_volume
                success_count += 1
                print("  OK")
            else:
                fail_count += 1
                print("  FAILED")

            if (i + 1) % 10 == 0:
                print(f"  >> Accumulated: ${total_volume:,.2f}")

            if i < args.rounds - 1:
                time.sleep(random.uniform(1, 2))

    else:
        print("=== NORMAL MODE ===")
        print(f"Market: {args.market}")
        print(f"Size: {size}")
        print(f"Direction policy: {args.direction}")
        print(f"Rounds: {args.rounds}")
        print(f"Max margin: {args.max_margin:.0%}")
        print(f"Hold time: {args.hold} min\n")

        # Build/hold/close: choose once for the full cycle when policy is random.
        policy = args.direction
        if policy == SIDE_POLICY_RANDOM:
            policy = SIDE_POLICY_ACCOUNT1_LONG if random.choice([True, False]) else SIDE_POLICY_ACCOUNT1_SHORT
            _log("INFO", f"Random direction chosen for this run: {policy}")

        long_acc, short_acc = choose_long_short_for_open(pair, policy)
        cookie_long = cookie_primary if long_acc is pair.primary else cookie_secondary
        cookie_short = cookie_secondary if short_acc is pair.secondary else cookie_primary

        run_normal_mode(
            long_acc,
            short_acc,
            cookie_long,
            cookie_short,
            args.market,
            size=size,
            max_margin=args.max_margin,
            hold_minutes=args.hold,
            max_rounds=args.rounds,
        )

    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("RUN SUMMARY")
    print("=" * 60)
    print(f"Duration: {elapsed/60:.1f} min")
    if args.instant_close:
        print(f"Rounds: {success_count} OK, {fail_count} failed")
        print(f"Total volume: ${total_volume:,.2f}")

    print("\nFinal positions:")
    for acc, cookie, instrument, _ in _active_accounts:
        pos = get_position_size(acc, cookie, instrument)
        status = "✓" if pos == 0 else "⚠"
        print(f"  {status} {acc.name}: {pos or 0}")
    print("=" * 60)


if __name__ == "__main__":
    main()
