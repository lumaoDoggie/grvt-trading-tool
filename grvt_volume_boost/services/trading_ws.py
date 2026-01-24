from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

import websockets

from grvt_volume_boost.clients.market_data import get_instrument, get_ticker
from grvt_volume_boost.clients.trades import post as trades_post
from grvt_volume_boost.services.orders import (
    build_create_order_payload,
    cancel_all_orders,
    cancel_order,
    get_position_size,
)
from grvt_volume_boost.sizing import mid_price_from_ticker, normalize_size
from grvt_volume_boost.settings import WS_URL
from grvt_volume_boost.util import deep_contains


def _decimal_field(inst_info: dict, key: str, default: str = "0") -> Decimal:
    raw = inst_info.get(key, default)
    if raw is None:
        raw = default
    return Decimal(str(raw))


def _deep_contains(obj: Any, needle: str) -> bool:
    # Back-compat alias for older call sites.
    return deep_contains(obj, needle)


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
    post_only: bool,
) -> dict:
    # Back-compat shim: this module previously had its own payload builder.
    if is_market:
        # `price` is ignored for market orders.
        return build_create_order_payload(
            acc=acc,
            instrument=instrument,
            size=size,
            inst_info=inst_info,
            is_buying=is_buying,
            is_market=True,
            time_in_force="GOOD_TILL_TIME",
            tif_code=1,
            price=Decimal("0"),
            reduce_only=reduce_only,
            post_only=post_only,
            nonce=nonce,
        )
    if price is None:
        raise ValueError("price is required for limit orders")
    return build_create_order_payload(
        acc=acc,
        instrument=instrument,
        size=size,
        inst_info=inst_info,
        is_buying=is_buying,
        is_market=False,
        time_in_force="GOOD_TILL_TIME",
        tif_code=1,
        price=price,
        reduce_only=reduce_only,
        post_only=post_only,
        nonce=nonce,
    )


async def ws_connect_and_subscribe(
    *,
    cookie: str,
    main_account_id: str,
    stream: str,
    selectors: list[str],
    request_id: int = 1,
):
    """Open an authenticated WS connection and send a JSON-RPC subscribe."""
    ws = await websockets.connect(
        WS_URL,
        extra_headers={
            "Cookie": f"gravity={cookie}",
            # Required per GRVT trading streams docs for account-specific feeds.
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
                "id": request_id,
            }
        )
    )
    return ws


async def listen_orders(
    *,
    cookie: str | None = None,
    cookie_getter: Callable[[], str | None] | None = None,
    main_account_id: str,
    sub_account_id: str,
    instrument: str | None,
    on_event: Callable[[str], None],
    stop_event,
) -> None:
    """Continuously listen to v1.order updates and push raw JSON to on_event()."""
    if cookie is None and cookie_getter is None:
        raise ValueError("Provide cookie or cookie_getter")
    selector = str(sub_account_id)
    if instrument and instrument.lower() != "all":
        selector = f"{sub_account_id}-{instrument}"

    while not stop_event.is_set():
        ws = None
        try:
            c = cookie_getter() if cookie_getter is not None else cookie
            if not c:
                on_event("[WS] Cookie refresh failed; retrying...\n")
                await asyncio.sleep(1.0)
                continue
            ws = await ws_connect_and_subscribe(
                cookie=c,
                main_account_id=main_account_id,
                stream="v1.order",
                selectors=[selector],
            )
            on_event(f"[WS] subscribed selector={selector}\n")
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except TimeoutError:
                    continue
                on_event(raw + "\n")
        except Exception as e:
            on_event(f"[WS] ERROR: {type(e).__name__}: {e}\n")
            await asyncio.sleep(1.0)
        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass


async def place_far_limit_then_cancel(
    *,
    acc,
    cookie: str,
    instrument: str,
    size: Decimal,
    is_buying: bool,
    on_event: Callable[[str], None],
) -> None:
    """Place a far-away post-only limit order, observe it on WS, then cancel it."""
    inst_info = get_instrument(instrument)
    ticker = get_ticker(instrument)
    mid = mid_price_from_ticker(ticker)
    tick = _decimal_field(inst_info, "tick_size", "0.0")
    min_notional = _decimal_field(inst_info, "min_notional", "0")

    # Far enough that it (very likely) won't execute.
    far_mult = Decimal("0.55") if is_buying else Decimal("1.80")
    far_price = mid * far_mult

    size = normalize_size(inst_info, size).size
    if min_notional > 0:
        min_price = (min_notional / size) * Decimal("1.02")  # slight buffer
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
        post_only=True,
    )

    on_event(
        f"[REST] create limit(postOnly): {instrument} side={'BUY' if is_buying else 'SELL'} "
        f"size={size} price={far_price} nonce={nonce}\n"
    )

    ws = await ws_connect_and_subscribe(
        cookie=cookie,
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
        on_event("[REST] create_order response:\n" + json.dumps(created, indent=2) + "\n")

        if r.status_code != 200:
            return

        # Build a few string needles to match WS payload (format varies).
        size_needles = {str(intent.size), str(intent.size.normalize()), f"{intent.size:f}".rstrip("0").rstrip(".")}
        price_needles = {str(intent.price), str(intent.price.normalize()), f"{intent.price:f}".rstrip("0").rstrip(".")}

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

            if _deep_contains(order_data, str(intent.nonce)) or (
                _deep_contains(order_data, intent.instrument)
                and any(_deep_contains(order_data, n) for n in price_needles)
                and any(_deep_contains(order_data, n) for n in size_needles)
            ):
                oid = _extract_oid(order_data)
                on_event("[WS] matched order update:\n" + json.dumps(order_data, indent=2) + "\n")
                break

        if not oid:
            if seen == 0:
                on_event("[WS] No order messages received after subscribe (auth/stream issue?)\n")
            else:
                on_event("[WS] Could not determine order_id/oid from WS.\n")
                if last_order_data:
                    on_event("[WS] Last order message seen:\n" + json.dumps(last_order_data, indent=2) + "\n")
            ok_all = cancel_all_orders(acc, cookie)
            on_event(f"[CANCEL] cancel_all_orders fallback => {ok_all}\n")
            return

        ok = cancel_order(acc, cookie, oid)
        on_event(f"[CANCEL] cancel_order({oid}) => {ok}\n")

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
                on_event("[WS] update after cancel:\n" + json.dumps(order_data, indent=2) + "\n")
                break
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def close_position_if_any(
    *,
    acc,
    cookie: str,
    instrument: str,
    on_event: Callable[[str], None],
) -> bool:
    """If there's a position, close it with a reduce-only market order (and observe it on WS)."""
    inst_info = get_instrument(instrument)
    pos = get_position_size(acc, cookie, instrument)
    if pos is None:
        raise RuntimeError("Failed to read position (auth/cookie issue?)")
    if pos == 0:
        on_event("[POS] No position to close.\n")
        return False

    size = abs(Decimal(pos))
    is_buying = pos < 0  # if short: buy to close; if long: sell to close
    nonce = random.randint(0, 2**32 - 1)
    on_event(
        f"[POS] Closing {instrument} pos={pos} with MARKET {'BUY' if is_buying else 'SELL'} "
        f"size={size} nonce={nonce}\n"
    )

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
        post_only=False,
    )

    ws = await ws_connect_and_subscribe(
        cookie=cookie,
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
        on_event("[REST] create_order response:\n" + json.dumps(created, indent=2) + "\n")

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
            if isinstance(order_data, dict) and _deep_contains(order_data, str(nonce)) and _deep_contains(order_data, instrument):
                on_event("[WS] matched close update:\n" + json.dumps(order_data, indent=2) + "\n")
                break
    finally:
        try:
            await ws.close()
        except Exception:
            pass

    return True
