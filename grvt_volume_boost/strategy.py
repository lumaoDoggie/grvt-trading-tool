from __future__ import annotations

import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import TYPE_CHECKING, Callable

from grvt_volume_boost.auth.cookies import get_fresh_cookie
from grvt_volume_boost.clients.market_data import get_instrument, get_ticker
from grvt_volume_boost.config import AccountConfig
from grvt_volume_boost.services.orders import (
    cancel_order,
    get_all_initial_leverage,
    get_margin_ratio,
    get_open_orders,
    get_position_size,
    place_ioc_order,
    place_limit_order,
    place_market_order,
    set_initial_leverage,
)
from grvt_volume_boost.ws import OrderStreamClient, wait_for_order_by_client_co_sync, wait_for_order_sync

if TYPE_CHECKING:
    from grvt_volume_boost.price_monitor import PriceBuffer


# Avoid spamming leverage changes on repeated "insufficient margin" retries.
_LAST_LEVERAGE_BUMP: dict[tuple[str, str], float] = {}


def _maybe_bump_initial_leverage(
    acc: AccountConfig,
    cookie: str,
    instrument: str,
    *,
    on_log: Callable[[str], None] | None = None,
) -> bool:
    """Best-effort: increase initial leverage for (subaccount, instrument) to reduce margin errors."""
    key = (str(acc.sub_account_id), str(instrument))
    now = time.time()
    last = _LAST_LEVERAGE_BUMP.get(key)
    if last is not None and (now - last) < 60.0:
        return False
    _LAST_LEVERAGE_BUMP[key] = now

    def _log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
                return
            except Exception:
                pass

    cur = None
    items = get_all_initial_leverage(acc, cookie)
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            inst = it.get("i") or it.get("instrument")
            if str(inst) != str(instrument):
                continue
            cur = it.get("l") or it.get("leverage")
            break

    # Try a few common max candidates. If the venue caps it lower, the call will fail and we fall through.
    candidates = ["50", "25", "20", "15", "10", "8", "5", "3"]
    if cur is not None:
        try:
            cur_f = float(cur)
            candidates = [c for c in candidates if float(c) > cur_f]
        except Exception:
            pass

    for lev in candidates:
        ok = set_initial_leverage(acc, cookie, instrument=instrument, leverage=lev)
        if ok:
            _log(f"Auto-set initial leverage: {instrument} -> {lev}x")
            return True

    _log("Auto-set initial leverage failed (no higher leverage accepted)")
    return False


def _decimal_field(inst_info: dict, key: str, default: str = "0") -> Decimal:
    raw = inst_info.get(key, default)
    if raw is None:
        raw = default
    return Decimal(str(raw))


def _size_quantum(inst_info: dict) -> Decimal:
    """Smallest expected position increment based on base_decimals."""
    try:
        base_decimals = int(inst_info.get("base_decimals", 9))
    except Exception:
        base_decimals = 9
    return Decimal(1) / (Decimal(10) ** base_decimals)


def _fmt_decimal(d: Decimal) -> str:
    """Stable, human-readable Decimal formatting (no scientific notation; trim trailing zeros)."""
    try:
        s = format(d, "f")
    except Exception:
        s = str(d)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _round_to_tick(price: Decimal, tick: Decimal, *, rounding) -> Decimal:
    if tick <= 0:
        return price
    steps = (price / tick).to_integral_value(rounding=rounding)
    return (steps * tick).quantize(tick, rounding=ROUND_DOWN)


def _choose_maker_price(*, best_bid: Decimal, best_ask: Decimal, tick: Decimal, is_buying: bool) -> Decimal:
    """Pick a post-only maker price that reduces external-fill risk.

    External fills happen when our taker IOC matches external liquidity at the same
    price before it reaches our maker order (FIFO queue at that price level).

    Using a rounded mid can collide with other LPs quoting at "natural" inside-spread
    ticks (often the mid). When the spread is at least 2 ticks, we instead place the
    maker 1 tick inside the spread (creating a less-crowded price level):
    - Maker BUY  => best_ask - tick
    - Maker SELL => best_bid + tick
    """
    if tick <= 0:
        return best_bid if is_buying else best_ask

    spread = best_ask - best_bid
    # When spread >= 2 ticks, there exists at least one inside tick level.
    try:
        spread_ticks = int((spread / tick).to_integral_value(rounding=ROUND_DOWN))
    except Exception:
        spread_ticks = 0
    if spread_ticks >= 2:
        return (best_ask - tick) if is_buying else (best_bid + tick)

    # 1-tick spread fallback: cannot pick an inside level; use mid rounding towards our side.
    mid = (best_bid + best_ask) / 2
    if is_buying:
        mid_rounded = (mid / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return max(mid_rounded, best_bid)
    mid_rounded = (mid / tick).to_integral_value(rounding=ROUND_UP) * tick
    return min(mid_rounded, best_ask)


def _extract_order_id(value: object) -> str | None:
    """Best-effort extraction of order id from varying GRVT response shapes."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        oid = value.get("oid") or value.get("order_id") or value.get("id") or value.get("oi")
        if oid:
            return str(oid)
        # Some responses nest the id.
        for k in ("r", "result", "data", "state", "feed"):
            if k in value:
                got = _extract_order_id(value.get(k))
                if got:
                    return got
        return None
    if isinstance(value, list):
        for item in value:
            got = _extract_order_id(item)
            if got:
                return got
    return None


def _cancel_open_orders_for_instrument(acc: "AccountConfig", cookie: str, instrument: str) -> int | None:
    """Cancel all open orders for an instrument. Returns count, or None on auth/API error."""
    pending = get_open_orders(acc, cookie, instrument)
    if pending is None:
        return None
    cancelled = 0
    for order in pending:
        oid = order.get("order_id") or order.get("oid") or order.get("id")
        if oid:
            cancel_order(acc, cookie, str(oid))
            cancelled += 1
    return cancelled


def _summarize_order_r(r: object) -> str:
    """Return a compact human-readable summary of an order response `r` (may be dict/str/etc)."""
    if r is None:
        return "r=None"
    if isinstance(r, str):
        return f"r={r}"
    if not isinstance(r, dict):
        return f"r_type={type(r).__name__}"

    # The response sometimes echoes our request payload, including signature fields; avoid dumping those.
    ti = r.get("ti")
    po = r.get("po")
    ro = r.get("ro")
    oid = r.get("oid") or r.get("order_id") or r.get("id") or r.get("oi")
    s1 = r.get("s1") if isinstance(r.get("s1"), dict) else {}
    status = s1.get("s") or s1.get("status")
    nonce = None
    m = r.get("m") if isinstance(r.get("m"), dict) else {}
    nonce = m.get("co") or r.get("co") or r.get("nonce")

    leg0 = None
    legs = r.get("l") if isinstance(r.get("l"), list) else (r.get("legs") if isinstance(r.get("legs"), list) else [])
    if legs:
        leg = legs[0] if isinstance(legs[0], dict) else {}
        inst = leg.get("i") or leg.get("instrument")
        size = leg.get("s") or leg.get("size")
        lp = leg.get("lp") or leg.get("limit_price")
        ib = leg.get("ib")
        side = "BUY" if ib is True else ("SELL" if ib is False else "?")
        leg0 = f"{inst} {side} size={size} lp={lp}"

    parts = []
    if oid is not None:
        parts.append(f"oid={oid}")
    if status:
        parts.append(f"status={status}")
    if ti is not None:
        parts.append(f"tif={ti}")
    if po is not None:
        parts.append(f"po={po}")
    if ro is not None:
        parts.append(f"ro={ro}")
    if nonce is not None:
        parts.append(f"co={nonce}")
    if leg0:
        parts.append(leg0)

    return " | ".join(parts) if parts else "r=dict"


def _extract_client_co(r: object) -> str | None:
    """Extract client order id (nonce) from create_order response `r` if present."""
    if not isinstance(r, dict):
        return None
    m = r.get("m")
    if isinstance(m, dict):
        co = m.get("co")
        if co is not None:
            return str(co)
    co = r.get("co") or r.get("nonce")
    return str(co) if co is not None else None


def check_price_stable(
    instrument: str,
    *,
    wait_sec: float = 1.0,
    tolerance: Decimal = Decimal("0.0001"),
    price_buffer: "PriceBuffer | None" = None,
) -> tuple[bool, Decimal, Decimal]:
    """Return (stable, best_bid, best_ask).
    
    If price_buffer is provided and has sufficient data (2+ seconds),
    uses the buffer for instant stability check. Otherwise falls back
    to blocking wait_sec observation.
    
    New stability definition: mid-price stayed within bid/ask range
    over the observation window.
    """
    # Try buffer-based check first (non-blocking)
    if price_buffer is not None and price_buffer.has_sufficient_data():
        latest = price_buffer.get_latest()
        if latest is not None:
            stable = price_buffer.is_stable(window_sec=2.0)
            return stable, latest.bid, latest.ask

    # Fallback: blocking observation
    ticker1 = get_ticker(instrument)
    bid1, ask1 = Decimal(ticker1["best_bid_price"]), Decimal(ticker1["best_ask_price"])
    mid1 = (bid1 + ask1) / 2

    time.sleep(wait_sec)

    ticker2 = get_ticker(instrument)
    bid2, ask2 = Decimal(ticker2["best_bid_price"]), Decimal(ticker2["best_ask_price"])

    # New stability check: did mid1 stay within bid2/ask2 bounds?
    # This means external trades didn't push price outside the range
    stable = bid2 <= mid1 <= ask2
    return stable, bid2, ask2


def extract_filled_size(order_result: dict | None) -> Decimal:
    """Extract the filled size from an order response.
    
    GRVT order response structure:
    - r.s1.bs = list of base sizes (filled amounts per leg)
    - r.s1.ts = list of total sizes (? sometimes used)
    
    Returns Decimal("0") if no fill info or parsing fails.
    """
    if not order_result:
        print(f"[DEBUG] extract_filled_size: no order_result")
        return Decimal("0")
    
    try:
        r = order_result.get("r", {})
        if not isinstance(r, dict):
            print(f"[DEBUG] extract_filled_size: r is not dict: {type(r)}")
            return Decimal("0")
        
        # Try s1.bs (base sizes - filled amounts)
        s1 = r.get("s1", {})
        if isinstance(s1, dict):
            bs = s1.get("bs", [])
            if bs and isinstance(bs, list) and len(bs) > 0:
                filled = Decimal(str(bs[0]))
                print(f"[DEBUG] extract_filled_size: bs[0]={bs[0]} -> {filled}")
                return filled
        
        # Alternative: check for filled_size or similar fields
        for key in ["filled_size", "filledSize", "fs"]:
            if key in r:
                filled = Decimal(str(r[key]))
                print(f"[DEBUG] extract_filled_size: {key}={r[key]} -> {filled}")
                return filled
        
        print(f"[DEBUG] extract_filled_size: no fill data found in response")
        return Decimal("0")
    except Exception as e:
        print(f"[DEBUG] extract_filled_size: exception {e}")
        return Decimal("0")


def _cancel_maker_order(
    acc: "AccountConfig",
    cookie: str,
    order_id: str,
    instrument: str,
    max_retries: int = 3,
) -> bool:
    """Cancel a maker order and verify it's actually gone.
    
    Returns True if order is confirmed cancelled/filled, False if still open after retries.
    """
    for attempt in range(max_retries):
        # Attempt cancel
        cancel_order(acc, cookie, order_id)
        time.sleep(0.3)  # Give exchange time to process
        
        # Verify order is gone by checking open orders
        pending = get_open_orders(acc, cookie, instrument)
        if pending is None:
            print(f"[DEBUG] _cancel_maker_order: failed to check open orders (attempt {attempt + 1})")
            continue
        
        # Check if our order is still in the list
        still_open = any(
            str(o.get("order_id") or o.get("oid") or o.get("id")) == str(order_id)
            for o in pending
        )
        
        if not still_open:
            if attempt > 0:
                print(f"[DEBUG] Maker order cancelled after {attempt + 1} attempts")
            return True
        
        print(f"[DEBUG] Maker order still open after cancel attempt {attempt + 1}, retrying...")
        time.sleep(0.5)
    
    print(f"[WARNING] Failed to cancel maker order {order_id} after {max_retries} attempts!")
    return False


def place_order_pair(
    acc_a: AccountConfig,
    acc_b: AccountConfig,
    cookie_a: str,
    cookie_b: str,
    instrument: str,
    inst_info: dict,
    size: Decimal,
    *,
    is_opening: bool,
    skip_stability: bool = False,
    price_buffer: "PriceBuffer | None" = None,
    on_log: Callable[[str], None] | None = None,
    ws_client: "OrderStreamClient | None" = None,
) -> tuple[bool, bool, str]:
    """Return (success, permanent_error, error_message)."""
    def _log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
                return
            except Exception:
                pass
        print(msg)

    tick_size = _decimal_field(inst_info, "tick_size", "0")
    reduce_only = not is_opening

    pos_a_before = get_position_size(acc_a, cookie_a, instrument)
    pos_b_before = get_position_size(acc_b, cookie_b, instrument)
    if pos_a_before is None or pos_b_before is None:
        return False, True, "Auth failed while reading positions"
    # get_position_size already returns Decimal; keep conversion safe anyway.
    pos_a_before = Decimal(str(pos_a_before))
    pos_b_before = Decimal(str(pos_b_before))

    if skip_stability:
        ticker = get_ticker(instrument)
        best_bid, best_ask = Decimal(ticker["best_bid_price"]), Decimal(ticker["best_ask_price"])
    else:
        stable, best_bid, best_ask = check_price_stable(
            instrument, wait_sec=1.0, price_buffer=price_buffer
        )
        if not stable:
            return False, False, "Price unstable"

    a_is_buying, b_is_buying = (True, False) if is_opening else (False, True)

    # Prefer a price 1 tick inside the spread (when possible) so our maker order
    # is top-of-book at a unique price and the taker leg is more likely to match it.
    maker_price = _choose_maker_price(best_bid=best_bid, best_ask=best_ask, tick=tick_size, is_buying=a_is_buying)
    maker_price = _round_to_tick(maker_price, tick_size, rounding=ROUND_DOWN if a_is_buying else ROUND_UP)
    # Guard: never cross the spread in post-only mode.
    if a_is_buying and maker_price >= best_ask:
        maker_price = _round_to_tick(best_bid, tick_size, rounding=ROUND_DOWN)
    if (not a_is_buying) and maker_price <= best_bid:
        maker_price = _round_to_tick(best_ask, tick_size, rounding=ROUND_UP)

    # Log a compact pricing line (avoid spamming full bid/ask objects).
    spread = best_ask - best_bid
    try:
        spread_ticks = int(spread / tick_size) if tick_size > 0 else 0
    except Exception:
        spread_ticks = 0
    _log(f"[DEBUG] price bid={best_bid} ask={best_ask} spread={spread} ticks={spread_ticks} maker_price={maker_price}")

    # Post-only prevents accidental taker fills; WS wait reduces race between maker and taker legs.
    maker_req_start = time.time()
    maker_result = place_limit_order(
        acc_a,
        cookie_a,
        instrument,
        inst_info,
        size,
        maker_price,
        a_is_buying,
        reduce_only=reduce_only,
        post_only=True,
    )
    maker_req_ms = (time.time() - maker_req_start) * 1000.0
    if not maker_result or not maker_result.get("r"):
        error_code = maker_result.get("c") if maker_result else None
        if error_code == 2080:
            # Best-effort: bump initial leverage and retry. This commonly happens when the
            # user's instrument leverage is set low (e.g., 10x) or equity is constrained.
            _maybe_bump_initial_leverage(acc_a, cookie_a, instrument, on_log=on_log)
            return False, False, "Insufficient margin"
        if error_code == 2002:
            return False, True, "Maker order failed: signature error"
        if error_code == 2066:
            return False, True, "Order size too small"
        return False, False, f"Maker order failed: {maker_result}"

    maker_r = maker_result.get("r")
    maker_order_id = _extract_order_id(maker_r) or _extract_order_id(maker_result)
    maker_co = _extract_client_co(maker_r)
    _log(f"[DEBUG] maker create_order req={maker_req_ms:.0f}ms: {_summarize_order_r(maker_r)}")
    
    _log(f"[DEBUG] Maker order placed: id={maker_order_id}, price={maker_price}, side={'BUY' if a_is_buying else 'SELL'}")

    # Only do WS sync if we have a real order ID (not '0x00' placeholder)
    maker_oid_valid = bool(maker_order_id) and str(maker_order_id) not in ("0x00", "0", "")
    ws_start = time.time()
    ws_ms: float | None = None
    ws_method = "none"
    # Prefer the persistent WS client when available. It's already subscribed, so it's much less
    # likely to miss the initial OPEN/PENDING event than a one-shot connect+subscribe after REST.
    on_book = False
    if ws_client is not None and ws_client.is_connected():
        on_book = ws_client.wait_for_maker_confirm(
            client_co=maker_co,
            instrument=instrument,
            size=str(size),
            price=str(maker_price),
            is_buying=a_is_buying,
            timeout=5.0,
        )
        ws_ms = (time.time() - ws_start) * 1000.0
        if on_book:
            ws_method = "persistent"
            _log(f"[DEBUG] maker ws_confirm method=persistent wait={ws_ms:.0f}ms")
        else:
            _log(f"[DEBUG] maker ws_confirm method=persistent wait={ws_ms:.0f}ms (not observed)")

    # Fallbacks (one-shot WS): needed if persistent WS is unavailable/stale, or maker_co missing.
    if not on_book and maker_oid_valid:
        ws_start2 = time.time()
        on_book = wait_for_order_sync(
            cookie_a,
            acc_a.sub_account_id,
            str(maker_order_id),
            main_account_id=acc_a.main_account_id,
            instrument=instrument,
            timeout=5.0,
        )
        ws_ms = (time.time() - ws_start2) * 1000.0
        if on_book:
            ws_method = "oid"
            _log(f"[DEBUG] maker ws_confirm method=oid wait={ws_ms:.0f}ms")
        else:
            _log(f"[DEBUG] maker ws_confirm method=oid wait={ws_ms:.0f}ms (not observed)")

    if not on_book and maker_co:
        ws_start3 = time.time()
        on_book = wait_for_order_by_client_co_sync(
            cookie_a,
            acc_a.sub_account_id,
            str(maker_co),
            main_account_id=acc_a.main_account_id,
            instrument=instrument,
            timeout=5.0,
        )
        ws_ms = (time.time() - ws_start3) * 1000.0
        if on_book:
            ws_method = f"co={maker_co}"
            _log(f"[DEBUG] maker ws_confirm method=co={maker_co} wait={ws_ms:.0f}ms")
        else:
            _log(f"[DEBUG] maker ws_confirm method=co={maker_co} wait={ws_ms:.0f}ms (not observed)")

    if not on_book:
        # If we can't confirm the maker, do not fire the IOC leg (it might hit external liquidity).
        if maker_oid_valid:
            _cancel_maker_order(acc_a, cookie_a, str(maker_order_id), instrument)
        _ = _cancel_open_orders_for_instrument(acc_a, cookie_a, instrument)
        return False, False, "Maker order not observed on book (WS); retrying to avoid external fill"

    # Record time gap from maker REST submit to firing the IOC leg.
    maker_to_ioc_ms = (time.time() - maker_req_start) * 1000.0
    _log(f"[DEBUG] maker->ioc gap={maker_to_ioc_ms:.0f}ms (maker_req={maker_req_ms:.0f}ms, ws_wait={(ws_ms or 0):.0f}ms, ws={ws_method})")

    ioc_start = time.time()
    taker_result = place_ioc_order(
        acc_b, cookie_b, instrument, inst_info, size, maker_price, b_is_buying, reduce_only=reduce_only
    )
    ioc_ms = (time.time() - ioc_start) * 1000.0
    _log(f"[DEBUG] taker create_order (IOC) req={ioc_ms:.0f}ms: {_summarize_order_r(taker_result.get('r') if isinstance(taker_result, dict) else taker_result)}")
    if not taker_result or not taker_result.get("r"):
        if maker_oid_valid:
            _cancel_maker_order(acc_a, cookie_a, str(maker_order_id), instrument)
        _ = _cancel_open_orders_for_instrument(acc_a, cookie_a, instrument)
        error_code = taker_result.get("c") if taker_result else None
        if error_code == 2080:
            _maybe_bump_initial_leverage(acc_b, cookie_b, instrument, on_log=on_log)
            return False, False, "Insufficient margin"
        if error_code == 2002:
            return False, True, "Taker order failed: signature error"
        if error_code == 2066:
            return False, True, "Order size too small"
        return False, False, f"Taker order failed: {taker_result}"
    # Always best-effort cancel any leftover maker orders on this instrument.
    # This protects against cases where the create_order response lacks a usable order id.
    if maker_oid_valid:
        _cancel_maker_order(acc_a, cookie_a, str(maker_order_id), instrument)
    _ = _cancel_open_orders_for_instrument(acc_a, cookie_a, instrument)

    # ===== VERIFY HEDGE BY CHECKING POSITION CHANGES =====
    # The taker might have filled someone else's order at the same price,
    # leaving our maker unfilled. Check that maker's position actually changed.
    # Positions can lag; poll briefly instead of a single fixed sleep.
    start = time.time()
    pos_a_after = None
    pos_b_after = None
    while time.time() - start < 2.0:
        pos_a_after = get_position_size(acc_a, cookie_a, instrument)
        pos_b_after = get_position_size(acc_b, cookie_b, instrument)
        if pos_a_after is None or pos_b_after is None:
            break
        if Decimal(str(pos_a_after)) != pos_a_before or Decimal(str(pos_b_after)) != pos_b_before:
            break
        time.sleep(0.2)
    if pos_a_after is None or pos_b_after is None:
        return False, True, "Auth failed while checking positions after trade"
    
    pos_a_after = Decimal(str(pos_a_after))
    pos_b_after = Decimal(str(pos_b_after))
    
    # Calculate expected position changes
    # Opening: acc_a buys (pos increases), acc_b sells (pos decreases)
    # Closing: acc_a sells (pos decreases), acc_b buys (pos increases)
    if is_opening:
        expected_a_delta = size  # acc_a should have gained
        expected_b_delta = -size  # acc_b should have lost
    else:
        expected_a_delta = -size
        expected_b_delta = size
    
    actual_a_delta = pos_a_after - pos_a_before
    actual_b_delta = pos_b_after - pos_b_before
    
    # Detect mismatches using smallest representable size increment to catch partial fills.
    min_size = _decimal_field(inst_info, "min_size", "0")
    delta_tol = max(_size_quantum(inst_info), Decimal("0.000000001"))
    
    # Check if maker's position changed as expected
    maker_delta_diff = abs(actual_a_delta - expected_a_delta)
    taker_delta_diff = abs(actual_b_delta - expected_b_delta)
    
    _log(
        "[DEBUG] Position deltas: "
        f"maker={_fmt_decimal(actual_a_delta)} (expected {_fmt_decimal(expected_a_delta)}), "
        f"taker={_fmt_decimal(actual_b_delta)} (expected {_fmt_decimal(expected_b_delta)})"
    )
    
    # If neither account position changed, the most likely causes are:
    # - Both orders were accepted but did not fill (IOC cancelled / maker cancelled)
    # - Position API is lagging behind
    # Treat this as a transient "no fill" and retry, rather than surfacing an external-fill warning.
    if abs(actual_a_delta) <= delta_tol and abs(actual_b_delta) <= delta_tol:
        _ = _cancel_open_orders_for_instrument(acc_a, cookie_a, instrument)
        _ = _cancel_open_orders_for_instrument(acc_b, cookie_b, instrument)
        return False, False, "No fills observed (positions unchanged); retrying"

    if maker_delta_diff > delta_tol or taker_delta_diff > delta_tol:
        # Position mismatch - taker likely filled someone else's order
        _log(f"[DEBUG] Position mismatch detected! Maker delta diff={maker_delta_diff}, Taker delta diff={taker_delta_diff}")

        # Helpful context: in 1-tick spread markets with large top-of-book depth, it's common
        # for the IOC leg to match external liquidity at the same price before it reaches our maker
        # (FIFO queue at best bid/ask).
        try:
            t = get_ticker(instrument)
            bb_sz = t.get("best_bid_size")
            ba_sz = t.get("best_ask_size")
            if bb_sz is not None or ba_sz is not None:
                _log(f"[DEBUG] top_of_book size bid={bb_sz} ask={ba_sz}")
        except Exception:
            pass
        
        # Cancel ALL pending orders for maker on this instrument (we don't have specific order ID)
        pending = get_open_orders(acc_a, cookie_a, instrument)
        if pending:
            _log(f"[DEBUG] Cancelling {len(pending)} pending maker orders...")
            for order in pending:
                oid = order.get("order_id") or order.get("oid") or order.get("id")
                if oid:
                    cancel_order(acc_a, cookie_a, str(oid))
        # Safety: also cancel any lingering open orders for the other account.
        _ = _cancel_open_orders_for_instrument(acc_b, cookie_b, instrument)
        
        imbalance = pos_a_after + pos_b_after
        
        # If we have net exposure, try to flatten it immediately. Use >= so that a net
        # position equal to the minimum size (common) gets closed too.
        close_threshold = min_size if min_size > 0 else delta_tol
        if abs(imbalance) >= close_threshold:
            # Close imbalance with market order
            if imbalance > 0:
                # Net long - need to sell
                if pos_a_after > 0:
                    _log(f"[DEBUG] Recovery: closing net long {imbalance} via {acc_a.name} market SELL reduce-only")
                    _ = place_market_order(acc_a, cookie_a, instrument, inst_info, abs(imbalance), is_buying=False, reduce_only=True)
                else:
                    _log(f"[DEBUG] Recovery: closing net long {imbalance} via {acc_b.name} market SELL reduce-only")
                    _ = place_market_order(acc_b, cookie_b, instrument, inst_info, abs(imbalance), is_buying=False, reduce_only=True)
            else:
                # Net short - need to buy
                if pos_a_after < 0:
                    _log(f"[DEBUG] Recovery: closing net short {imbalance} via {acc_a.name} market BUY reduce-only")
                    _ = place_market_order(acc_a, cookie_a, instrument, inst_info, abs(imbalance), is_buying=True, reduce_only=True)
                else:
                    _log(f"[DEBUG] Recovery: closing net short {imbalance} via {acc_b.name} market BUY reduce-only")
                    _ = place_market_order(acc_b, cookie_b, instrument, inst_info, abs(imbalance), is_buying=True, reduce_only=True)
            
            time.sleep(0.3)

        # Log post-recovery state (helps explain why the warning appeared even if we're now flat).
        try:
            final_a = get_position_size(acc_a, cookie_a, instrument)
            final_b = get_position_size(acc_b, cookie_b, instrument)
            if final_a is not None and final_b is not None:
                final_a_d = Decimal(str(final_a))
                final_b_d = Decimal(str(final_b))
                _log(f"[DEBUG] Post-recovery positions: {acc_a.name}={final_a_d}, {acc_b.name}={final_b_d}")
        except Exception:
            pass
        
        # Signal warning to GUI
        return (
            True,
            False,
            "EXTERNAL_FILL_WARNING: Position mismatch after IOC - "
            f"maker delta={_fmt_decimal(actual_a_delta)} expected={_fmt_decimal(expected_a_delta)} diff={_fmt_decimal(maker_delta_diff)}, "
            f"taker delta={_fmt_decimal(actual_b_delta)} expected={_fmt_decimal(expected_b_delta)} diff={_fmt_decimal(taker_delta_diff)}, "
            f"tol={_fmt_decimal(delta_tol)}",
        )
    
    # Full hedge - success!
    return True, False, ""


def place_order_pair_with_retry(
    acc_a: AccountConfig,
    acc_b: AccountConfig,
    cookie_a: str,
    cookie_b: str,
    instrument: str,
    inst_info: dict,
    size: Decimal,
    *,
    is_opening: bool,
    skip_stability: bool = False,
    max_retries: int = 3,
    on_log: Callable[[str], None] | None = None,
    price_buffer: "PriceBuffer | None" = None,
    ws_client: "OrderStreamClient | None" = None,
) -> tuple[bool, str]:
    last_error = ""
    for attempt in range(max_retries):
        success, permanent_error, error_msg = place_order_pair(
            acc_a,
            acc_b,
            cookie_a,
            cookie_b,
            instrument,
            inst_info,
            size,
            is_opening=is_opening,
            skip_stability=skip_stability,
            price_buffer=price_buffer,
            on_log=on_log,
            ws_client=ws_client,
        )
        if success:
            # Pass through warning message (e.g., EXTERNAL_FILL_WARNING) if present
            return True, error_msg or ""
        if permanent_error:
            return False, error_msg

        last_error = error_msg or "Unknown error"
        if attempt < max_retries - 1:
            wait = 2**attempt
            msg = f"    Retry {attempt+2}/{max_retries} in {wait}s ({last_error})..."
            if on_log:
                try:
                    on_log(msg)
                except Exception:
                    pass
            else:
                print(msg)
            time.sleep(wait)

    return False, f"Failed after {max_retries} retries: {last_error}"


def market_close(acc: AccountConfig, cookie: str, instrument: str, inst_info: dict) -> None:
    pos_size = get_position_size(acc, cookie, instrument)
    if pos_size is None or pos_size == 0:
        return

    size = abs(pos_size)
    is_long = pos_size > 0
    place_market_order(acc, cookie, instrument, inst_info, size, is_buying=not is_long, reduce_only=True)


def run_instant_round(
    acc_a: AccountConfig,
    acc_b: AccountConfig,
    cookie_a: str,
    cookie_b: str,
    instrument: str,
    inst_info: dict,
    size: Decimal,
    *,
    delay: float,
) -> tuple[bool, Decimal, str]:
    ticker = get_ticker(instrument)
    mid_price = (Decimal(ticker["best_bid_price"]) + Decimal(ticker["best_ask_price"])) / 2

    success, error_msg = place_order_pair_with_retry(
        acc_a,
        acc_b,
        cookie_a,
        cookie_b,
        instrument,
        inst_info,
        size,
        is_opening=True,
        skip_stability=False,
    )
    if not success:
        return False, mid_price, error_msg or "Open failed"

    time.sleep(delay)

    success, error_msg = place_order_pair_with_retry(
        acc_a,
        acc_b,
        cookie_a,
        cookie_b,
        instrument,
        inst_info,
        size,
        is_opening=False,
        skip_stability=False,
    )
    if not success:
        return False, mid_price, error_msg or "CLOSE FAILED - positions may be open!"

    return True, mid_price, ""


def run_cycle(
    acc_a: AccountConfig,
    acc_b: AccountConfig,
    cookie_a: str,
    cookie_b: str,
    instrument: str,
    *,
    size: Decimal,
    max_margin: float,
    hold_minutes: int,
) -> None:
    """Deprecated: single open/hold/close cycle."""
    inst_info = get_instrument(instrument)

    margin_a = get_margin_ratio(acc_a, cookie_a)
    margin_b = get_margin_ratio(acc_b, cookie_b)
    if margin_a is None or margin_b is None:
        print("  ERROR: Failed to get margin, skipping...")
        return

    print(f"  Margin: A={margin_a:.1%}, B={margin_b:.1%}")
    if margin_a > max_margin or margin_b > max_margin:
        print("  Margin too high, skipping...")
        return

    print(f"  Opening {size} {instrument}...")
    success, _, _ = place_order_pair(
        acc_a, acc_b, cookie_a, cookie_b, instrument, inst_info, size, is_opening=True
    )
    if not success:
        print("  Failed to open")
        return

    print(f"  Holding for {hold_minutes} minutes...")
    time.sleep(hold_minutes * 60)

    print("  Closing...")
    success, _, _ = place_order_pair(
        acc_a, acc_b, cookie_a, cookie_b, instrument, inst_info, size, is_opening=False
    )
    if not success:
        print("  Limit close failed, using market close...")
        market_close(acc_a, cookie_a, instrument, inst_info)
        market_close(acc_b, cookie_b, instrument, inst_info)

    print("  Done")


def run_normal_mode(
    acc_a: AccountConfig,
    acc_b: AccountConfig,
    cookie_a: str,
    cookie_b: str,
    instrument: str,
    *,
    size: Decimal,
    max_margin: float,
    hold_minutes: int,
    max_rounds: int,
) -> None:
    inst_info = get_instrument(instrument)
    opened_rounds = 0

    print("=== PHASE 1: BUILDING POSITIONS ===")
    for i in range(max_rounds):
        margin_a = get_margin_ratio(acc_a, cookie_a)
        margin_b = get_margin_ratio(acc_b, cookie_b)
        if margin_a is None or margin_b is None:
            print(f"[Round {i+1}] ERROR: Failed to get margin, stopping")
            break
        print(f"[Round {i+1}] Margin: A={margin_a:.1%}, B={margin_b:.1%}")

        if margin_a > max_margin or margin_b > max_margin:
            print("  Max margin reached, stopping build-up")
            break

        print(f"  Opening {size} {instrument}...")
        success, permanent_error, error_msg = place_order_pair(
            acc_a,
            acc_b,
            cookie_a,
            cookie_b,
            instrument,
            inst_info,
            size,
            is_opening=True,
        )
        if not success:
            if permanent_error or error_msg:
                print(f"  CRITICAL: {error_msg}, stopping immediately")
            else:
                print("  Failed to open, stopping build-up")
            break

        opened_rounds += 1
        print("  OK")
        time.sleep(1)

    if opened_rounds == 0:
        print("No positions opened")
        return

    print(f"\n=== PHASE 2: HOLDING ({hold_minutes} min) ===")
    print(f"Opened {opened_rounds} rounds, total size: {size * opened_rounds}")
    time.sleep(hold_minutes * 60)

    print("\nRefreshing cookies before close...")
    cookie_a = get_fresh_cookie(acc_a.browser_state_path) or cookie_a
    cookie_b = get_fresh_cookie(acc_b.browser_state_path) or cookie_b

    print("\n=== PHASE 3: CLOSING POSITIONS ===")
    for i in range(opened_rounds):
        print(f"[Close {i+1}/{opened_rounds}] Closing {size}...")
        success, _, error_msg = place_order_pair(
            acc_a,
            acc_b,
            cookie_a,
            cookie_b,
            instrument,
            inst_info,
            size,
            is_opening=False,
        )
        if not success:
            print(f"  Limit close failed ({error_msg}), using market close...")
            market_close(acc_a, cookie_a, instrument, inst_info)
            market_close(acc_b, cookie_b, instrument, inst_info)
        else:
            print("  OK")
        time.sleep(1)

    print("\nNormal mode complete")
