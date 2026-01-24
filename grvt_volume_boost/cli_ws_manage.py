from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

import websockets
from dotenv import load_dotenv

from grvt_volume_boost.auth.cookies import load_cookie_cache
from grvt_volume_boost.clients.market_data import get_instrument, get_ticker
from grvt_volume_boost.clients.trades import post as trades_post
from grvt_volume_boost.config import get_account
from grvt_volume_boost.services.orders import cancel_all_orders, cancel_order, get_position_size
from grvt_volume_boost.services.signing import sign_order
from grvt_volume_boost.sizing import mid_price_from_ticker, normalize_size
from grvt_volume_boost.settings import WS_URL
from grvt_volume_boost.ws_compat import connect as ws_connect


def _decimal_field(inst_info: dict, key: str, default: str = "0") -> Decimal:
    raw = inst_info.get(key, default)
    if raw is None:
        raw = default
    return Decimal(str(raw))


def _round_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    steps = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return (steps * tick).quantize(tick, rounding=ROUND_DOWN)


def _deep_contains(obj: Any, needle: str) -> bool:
    """Best-effort recursive search for `needle` in dict/list/strings."""
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
            if _deep_contains(v, needle):
                return True
        return False
    if isinstance(obj, list):
        return any(_deep_contains(v, needle) for v in obj)
    return False


def _extract_oid(order_data: dict) -> str | None:
    feed = order_data.get("feed") if isinstance(order_data, dict) else None
    if isinstance(feed, dict):
        oid = feed.get("order_id") or feed.get("oid") or feed.get("id")
        if oid:
            return str(oid)

    oid = order_data.get("order_id") or order_data.get("oid") or order_data.get("id")
    return str(oid) if oid else None


@dataclass(frozen=True)
class LimitOrderIntent:
    instrument: str
    size: Decimal
    is_buying: bool
    price: Decimal
    nonce: int


def _build_order_payload(
    *,
    acc,
    instrument: str,
    size: Decimal,
    is_buying: bool,
    nonce: int,
    inst_info: dict,
    is_market: bool,
    reduce_only: bool,
    price: Decimal | None,
) -> dict:
    base_decimals = int(inst_info["base_decimals"])
    inst_hash = inst_info["instrument_hash"]
    asset_id = int(inst_hash, 16) if str(inst_hash).startswith("0x") else int(inst_hash)

    contract_size = int((size * (Decimal(10) ** base_decimals)).to_integral_value(rounding=ROUND_DOWN))
    limit_price = 0
    if not is_market:
        if price is None:
            raise ValueError("price is required for limit orders")
        limit_price = int((price * (Decimal(10) ** 9)).to_integral_value(rounding=ROUND_DOWN))  # 9 decimals

    expiration_ns = int((time.time() + 30 * 24 * 60 * 60) * 1000) * 1_000_000

    message_data = {
        "subAccountID": int(acc.sub_account_id),
        "isMarket": bool(is_market),
        "timeInForce": 1,  # GOOD_TILL_TIME
        "postOnly": False,
        "reduceOnly": bool(reduce_only),
        "legs": [
            {
                "assetID": asset_id,
                "contractSize": contract_size,
                "limitPrice": limit_price,
                "isBuyingContract": bool(is_buying),
            }
        ],
        "nonce": int(nonce),
        "expiration": int(expiration_ns),
    }

    _, sig = sign_order(acc, message_data)

    return {
        "o": {
            "sa": acc.sub_account_id,
            "im": bool(is_market),
            "ti": "GOOD_TILL_TIME",
            "po": False,
            "ro": bool(reduce_only),
            "l": [
                {
                    "i": instrument,
                    "s": str(size),
                    "lp": "0.0" if is_market else str(price),
                    "ib": bool(is_buying),
                }
            ],
            "s": sig,
            # Match existing clients: "co" holds the same nonce.
            "m": {"s": "WEB", "co": str(nonce)},
        }
    }


async def _ws_connect_and_subscribe(cookie: str, *, main_account_id: str, stream: str, selectors: list[str]):
    ws = await ws_connect(
        WS_URL,
        headers={
            "Cookie": f"gravity={cookie}",
            # Required per GRVT trading streams docs.
            "X-Grvt-Account-Id": str(main_account_id),
        },
        close_timeout=2,
    )
    await ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {"stream": stream, "selectors": selectors},
                "id": 1,
            }
        )
    )
    return ws


async def _place_limit_then_cancel(*, acc, cookie: str, instrument: str, size: Decimal, is_buying: bool) -> None:
    inst_info = get_instrument(instrument)
    ticker = get_ticker(instrument)
    mid = mid_price_from_ticker(ticker)
    tick = _decimal_field(inst_info, "tick_size", "0.0")
    min_notional = _decimal_field(inst_info, "min_notional", "0")

    # Far away so it (very likely) won't execute.
    far_mult = Decimal("0.55") if is_buying else Decimal("1.80")
    far_price = mid * far_mult

    # Must satisfy minimum notional constraints, otherwise the order is rejected.
    # For buys: pick the lowest price that still meets min_notional to keep it "far" from mid.
    # For sells: keep it well above mid and min_notional is always satisfied when price is higher.
    size = normalize_size(inst_info, size).size
    if min_notional > 0:
        min_price = (min_notional / size) * Decimal("1.02")  # slight buffer
        if is_buying:
            far_price = max(far_price, min_price)
        else:
            far_price = max(far_price, min_price)

    far_price = _round_to_tick(far_price, tick)
    if far_price <= 0:
        raise RuntimeError(f"Computed far price invalid: {far_price}")

    nonce = random.randint(0, 2**32 - 1)
    intent = LimitOrderIntent(instrument=instrument, size=size, is_buying=is_buying, price=far_price, nonce=nonce)

    payload = _build_order_payload(
        acc=acc,
        instrument=intent.instrument,
        size=intent.size,
        is_buying=intent.is_buying,
        nonce=intent.nonce,
        inst_info=inst_info,
        is_market=False,
        reduce_only=False,
        price=intent.price,
    )
    print(f"[REST] Creating limit order: {instrument} side={'BUY' if is_buying else 'SELL'} size={size} price={far_price} nonce={nonce}")

    ws = await _ws_connect_and_subscribe(
        cookie,
        main_account_id=acc.main_account_id,
        stream="v1.order",
        selectors=[f"{acc.sub_account_id}-{instrument}"],
    )
    try:
        r = trades_post("/lite/v1/create_order", acc=acc, cookie=cookie, payload=payload, timeout=30)
        try:
            created = r.json()
        except Exception:
            created = {"status_code": r.status_code, "text": r.text}
        print("[REST] create_order response:")
        print(json.dumps(created, indent=2))

        # If the order was rejected, don't wait on WS for it.
        if r.status_code != 200 or (isinstance(created, dict) and created.get("s") in (400, 401, 403)):
            return

        # Build a few string "needles" to match the WS payload (format varies).
        size_needles = {str(intent.size), str(intent.size.normalize()), f"{intent.size:f}".rstrip("0").rstrip(".")}
        price_needles = {str(intent.price), str(intent.price.normalize()), f"{intent.price:f}".rstrip("0").rstrip(".")}
        needles = {str(intent.nonce), intent.instrument, *size_needles, *price_needles}

        oid: str | None = None
        start = time.time()
        seen = 0
        last_order_data: dict | None = None
        while time.time() - start < 15.0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            order_data = msg.get("params", {}).get("result", msg.get("result", msg))
            if not isinstance(order_data, dict):
                continue
            seen += 1
            last_order_data = order_data

            # Prefer matching by nonce/client-order-id, but fall back to matching by instrument+price+size.
            if _deep_contains(order_data, str(intent.nonce)) or (
                _deep_contains(order_data, intent.instrument)
                and any(_deep_contains(order_data, n) for n in price_needles)
                and any(_deep_contains(order_data, n) for n in size_needles)
            ):
                oid = _extract_oid(order_data)
                print("[WS] Matched order update:")
                print(json.dumps(order_data, indent=2))
                break

        if not oid:
            if seen == 0:
                print("[WS] No order messages received after subscribe (auth/stream issue?).")
            else:
                print("[WS] Could not determine order_id/oid from WS.")
                if last_order_data:
                    print("[WS] Last order message seen:")
                    print(json.dumps(last_order_data, indent=2))

            # Fallback: cancel all open orders for this sub-account (usually only the one we just created).
            ok_all = cancel_all_orders(acc, cookie)
            print(f"[CANCEL] cancel_all_orders() fallback => {ok_all}")
            return

        ok = cancel_order(acc, cookie, oid)
        print(f"[CANCEL] cancel_order({oid}) => {ok}")

        start = time.time()
        while time.time() - start < 10.0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            order_data = msg.get("params", {}).get("result", msg.get("result", msg))
            if isinstance(order_data, dict) and _deep_contains(order_data, oid):
                print("[WS] Order update after cancel:")
                print(json.dumps(order_data, indent=2))
                break
    finally:
        await ws.close()


async def _close_position_if_any(*, acc, cookie: str, instrument: str) -> bool:
    inst_info = get_instrument(instrument)
    pos = get_position_size(acc, cookie, instrument)
    if pos is None:
        raise RuntimeError("Failed to read position (auth/cookie issue?)")
    if pos == 0:
        print("[POS] No position to close.")
        return False

    size = abs(Decimal(pos))
    is_buying = pos < 0  # if short, buy to close; if long, sell to close
    print(f"[POS] Closing position: {instrument} pos={pos} with MARKET {'BUY' if is_buying else 'SELL'} size={size}")
    nonce = random.randint(0, 2**32 - 1)
    payload = _build_order_payload(
        acc=acc,
        instrument=instrument,
        size=size,
        is_buying=is_buying,
        nonce=nonce,
        inst_info=inst_info,
        is_market=True,
        reduce_only=True,
        price=None,
    )

    ws = await _ws_connect_and_subscribe(
        cookie,
        main_account_id=acc.main_account_id,
        stream="v1.order",
        selectors=[f"{acc.sub_account_id}-{instrument}"],
    )
    try:
        r = trades_post("/lite/v1/create_order", acc=acc, cookie=cookie, payload=payload, timeout=30)
        try:
            created = r.json()
        except Exception:
            created = {"status_code": r.status_code, "text": r.text}
        print("[REST] create_order response:")
        print(json.dumps(created, indent=2))

        start = time.time()
        while time.time() - start < 10.0:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            order_data = msg.get("params", {}).get("result", msg.get("result", msg))
            if isinstance(order_data, dict) and _deep_contains(order_data, str(nonce)) and _deep_contains(order_data, instrument):
                print("[WS] Matched close order update:")
                print(json.dumps(order_data, indent=2))
                break
    finally:
        await ws.close()

    return True


def main(argv: list[str] | None = None) -> int:
    load_dotenv(".env")

    p = argparse.ArgumentParser(description="Use authenticated WS to observe orders; close or place/cancel a far limit.")
    p.add_argument("--account", type=int, choices=[1, 2], default=1)
    p.add_argument("--market", type=str, default="BTC_USDT_Perp")
    p.add_argument("--size", type=str, default="0.002")
    p.add_argument("--side", type=str, choices=["buy", "sell"], default="buy")
    p.add_argument("--mode", choices=["close-or-limit-cancel", "limit-cancel", "close"], default="close-or-limit-cancel")
    args = p.parse_args(argv)

    acc = get_account(args.account)
    cookie = load_cookie_cache(max_age_sec=60 * 60)  # allow up to 1h; cookie may still be valid
    if not cookie:
        print("No cached gravity cookie found. Re-login first (QR).")
        return 2

    instrument = args.market
    size = Decimal(args.size)
    is_buying = args.side == "buy"

    if args.mode in ("close-or-limit-cancel", "close"):
        closed = asyncio.run(_close_position_if_any(acc=acc, cookie=cookie, instrument=instrument))
        if closed:
            return 0
        if args.mode == "close":
            return 0

    asyncio.run(_place_limit_then_cancel(acc=acc, cookie=cookie, instrument=instrument, size=size, is_buying=is_buying))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
