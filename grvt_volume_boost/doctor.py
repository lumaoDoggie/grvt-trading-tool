from __future__ import annotations

import time

from grvt_volume_boost.auth.cookies import get_fresh_cookie
from grvt_volume_boost.clients.market_data import get_instrument
from grvt_volume_boost.config import get_all_accounts
from grvt_volume_boost.services.orders import get_margin_ratio
from grvt_volume_boost.settings import CHAIN_ID, TRADES_URL


def run_doctor() -> bool:
    """Preflight check - validate config, accounts, cookies, and API connectivity."""
    print("=" * 60)
    print("GRVT DOCTOR - Preflight Check")
    print("=" * 60)

    errors: list[str] = []

    print("\n[1/4] Checking accounts...")
    accounts = []
    try:
        accounts, acc_errors = get_all_accounts()
        if acc_errors:
            errors.extend(acc_errors)
        if len(accounts) < 2:
            errors.append("Need 2 accounts configured")
        else:
            for acc in accounts:
                print(f"  ✓ {acc.name}: sub_account={acc.sub_account_id}")
    except Exception as e:
        errors.append(f"Account config error: {e}")

    print("\n[2/4] Checking browser state...")
    for acc in accounts:
        if acc.browser_state_path.exists():
            age_hours = (time.time() - acc.browser_state_path.stat().st_mtime) / 3600
            print(f"  ✓ {acc.name}: {acc.browser_state_path.name} (age: {age_hours:.1f}h)")
        else:
            errors.append(f"{acc.name}: browser state not found at {acc.browser_state_path}")

    print("\n[3/4] Getting fresh cookies...")
    cookies: dict[str, str] = {}
    for acc in accounts:
        cookie = get_fresh_cookie(acc.browser_state_path)
        if cookie:
            cookies[acc.name] = cookie
            print(f"  ✓ {acc.name}: cookie OK (len={len(cookie)})")
        else:
            errors.append(f"{acc.name}: failed to get cookie")

    print("\n[4/4] Testing API connectivity...")
    try:
        get_instrument("BTC_USDT_Perp")
        print("  ✓ Market data API: OK")
    except Exception as e:
        errors.append(f"Market data API error: {e}")

    for acc in accounts:
        cookie = cookies.get(acc.name)
        if not cookie:
            continue
        margin = get_margin_ratio(acc, cookie)
        if margin is not None:
            print(f"  ✓ {acc.name} margin ratio: {margin:.1%}")
        else:
            errors.append(f"{acc.name}: failed to get margin (API auth issue?)")

    print("\n" + "=" * 60)
    if errors:
        print(f"FAILED - {len(errors)} error(s):")
        for e in errors:
            print(f"  ✗ {e}")
        return False

    print("ALL CHECKS PASSED")
    print(f"Chain ID: {CHAIN_ID} ({'PRODUCTION' if CHAIN_ID == 325 else 'TESTNET'})")
    print(f"Trades URL: {TRADES_URL}")
    return True
