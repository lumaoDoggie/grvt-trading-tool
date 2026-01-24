from __future__ import annotations

import random
import time
from decimal import Decimal, ROUND_DOWN

from grvt_volume_boost.clients.trades import post
from grvt_volume_boost.config import AccountConfig
from grvt_volume_boost.logging_utils import debug
from grvt_volume_boost.services.signing import sign_order


def ping_auth(acc: AccountConfig, cookie: str) -> bool:
    """Cheap auth check for cookie/account headers."""
    try:
        r = post("/lite/v1/account_summary", acc=acc, cookie=cookie, payload={"sa": acc.sub_account_id}, timeout=15)
        if r.status_code != 200:
            return False
        data = r.json()
        return isinstance(data, dict) and ("r" in data or "result" in data)
    except Exception as e:
        debug("ping_auth failed", exc=e)
        return False


def get_margin_ratio(acc: AccountConfig, cookie: str) -> float | None:
    """Get current margin ratio (maintenance margin / equity). Returns None on error."""
    try:
        r = post(
            "/full/v1/account_summary",
            acc=acc,
            cookie=cookie,
            payload={"sub_account_id": acc.sub_account_id},
            timeout=30,
        )
        data = r.json()
        if "result" not in data:
            return None
        equity = Decimal(data["result"].get("total_equity") or data["result"].get("equity") or "0")
        margin = Decimal(data["result"].get("maintenance_margin") or "0")
        return float(margin / equity) if equity > 0 else 0.0
    except Exception as e:
        debug("get_margin_ratio failed", exc=e)
        return None


def get_position_size(acc: AccountConfig, cookie: str, instrument: str) -> Decimal | None:
    """Get current position size for an instrument. Returns None on error."""
    try:
        r = post(
            "/full/v1/positions",
            acc=acc,
            cookie=cookie,
            payload={"sub_account_id": acc.sub_account_id},
            timeout=30,
        )
        data = r.json()
        if "result" not in data:
            return None
        for p in data["result"]:
            if p.get("instrument") == instrument:
                return Decimal(p.get("size", "0"))
        return Decimal(0)
    except Exception as e:
        debug("get_position_size failed", exc=e, extra={"instrument": instrument})
        return None


def cancel_order(acc: AccountConfig, cookie: str, order_id: str) -> bool:
    """Cancel an order by ID."""
    try:
        r = post(
            "/full/v1/cancel_order",
            acc=acc,
            cookie=cookie,
            payload={"order_id": order_id, "sub_account_id": acc.sub_account_id},
            timeout=30,
        )
        return r.status_code == 200 and "result" in r.json()
    except Exception as e:
        debug("cancel_order failed", exc=e)
        return False


def cancel_all_orders(acc: AccountConfig, cookie: str) -> bool:
    """Cancel all orders for account."""
    try:
        r = post(
            "/full/v1/cancel_all_orders",
            acc=acc,
            cookie=cookie,
            payload={"sub_account_id": acc.sub_account_id},
            timeout=30,
        )
        return r.status_code == 200
    except Exception as e:
        debug("cancel_all_orders failed", exc=e)
        return False


def get_open_orders(acc: AccountConfig, cookie: str, instrument: str | None = None) -> list[dict] | None:
    """Get open orders for account, optionally filtered by instrument. Returns None on error."""
    try:
        payload: dict = {"sub_account_id": acc.sub_account_id}
        if instrument:
            payload["instrument"] = instrument
        r = post("/full/v1/open_orders", acc=acc, cookie=cookie, payload=payload, timeout=30)
        data = r.json()
        if "result" in data:
            return data["result"]
        return []
    except Exception as e:
        debug("get_open_orders failed", exc=e, extra={"instrument": instrument or ""})
        return None


def get_all_initial_leverage(acc: AccountConfig, cookie: str) -> list[dict] | None:
    """Return the account's configured initial leverage list (per instrument).

    Shape: [{"i": "...", "l": "10"}, ...] on lite, or [{"instrument": "...", "leverage": "10"}, ...] on full.
    Returns None on error.
    """
    try:
        r = post(
            "/lite/v1/get_all_initial_leverage",
            acc=acc,
            cookie=cookie,
            payload={"sa": acc.sub_account_id},
            timeout=30,
        )
        data = r.json()
        res = data.get("r") if isinstance(data, dict) else None
        if isinstance(res, list):
            return res
        # Some environments may return "result" instead.
        res2 = data.get("result") if isinstance(data, dict) else None
        if isinstance(res2, list):
            return res2
        return []
    except Exception as e:
        debug("get_all_initial_leverage failed", exc=e)
        return None


def set_initial_leverage(acc: AccountConfig, cookie: str, *, instrument: str, leverage: str) -> bool:
    """Set initial leverage for a given instrument on this subaccount. Returns True on success."""
    try:
        r = post(
            "/lite/v1/set_initial_leverage",
            acc=acc,
            cookie=cookie,
            payload={"sa": acc.sub_account_id, "i": instrument, "l": str(leverage)},
            timeout=30,
        )
        data = r.json()
        # Lite response shape for this endpoint is often {"s": true}.
        if r.status_code != 200 or not isinstance(data, dict):
            return False
        if data.get("s") is True:
            return True
        return "r" in data or "result" in data
    except Exception as e:
        debug("set_initial_leverage failed", exc=e, extra={"instrument": instrument, "leverage": leverage})
        return False


def build_create_order_payload(
    *,
    acc: AccountConfig,
    instrument: str,
    size: Decimal,
    inst_info: dict,
    is_buying: bool,
    is_market: bool,
    time_in_force: str,
    tif_code: int,
    price: Decimal,
    reduce_only: bool,
    post_only: bool = False,
    nonce: int | None = None,
    expiration_ns: int | None = None,
) -> dict:
    base_decimals = int(inst_info["base_decimals"])
    inst_hash = inst_info["instrument_hash"]
    asset_id = int(inst_hash, 16) if str(inst_hash).startswith("0x") else int(inst_hash)

    contract_size = int((size * (Decimal(10) ** base_decimals)).to_integral_value(rounding=ROUND_DOWN))

    limit_price = 0
    if not is_market:
        limit_price = int((price * (Decimal(10) ** 9)).to_integral_value(rounding=ROUND_DOWN))  # 9 decimals

    if nonce is None:
        nonce = random.randint(0, 2**32 - 1)
    if expiration_ns is None:
        # Keep the existing behavior: market uses ns precision; limit uses ms precision.
        if is_market:
            expiration_ns = int(time.time_ns() + 30 * 24 * 60 * 60 * 1_000_000_000)
        else:
            expiration_ns = int((time.time() + 30 * 24 * 60 * 60) * 1000) * 1_000_000

    message_data = {
        "subAccountID": int(acc.sub_account_id),
        "isMarket": is_market,
        "timeInForce": tif_code,
        "postOnly": bool(post_only),
        "reduceOnly": reduce_only,
        "legs": [
            {
                "assetID": asset_id,
                "contractSize": contract_size,
                "limitPrice": limit_price,
                "isBuyingContract": is_buying,
            }
        ],
        "nonce": nonce,
        "expiration": expiration_ns,
    }

    _, sig = sign_order(acc, message_data)

    return {
        "o": {
            "sa": acc.sub_account_id,
            "im": is_market,
            "ti": time_in_force,
            "po": bool(post_only),
            "ro": reduce_only,
            "l": [{"i": instrument, "s": str(size), "lp": "0" if is_market else str(price), "ib": is_buying}],
            "s": sig,
            "m": {"s": "WEB", "co": str(nonce)},
        }
    }


def _build_order_payload(
    *,
    acc: AccountConfig,
    instrument: str,
    size: Decimal,
    inst_info: dict,
    is_buying: bool,
    is_market: bool,
    time_in_force: str,
    tif_code: int,
    price: Decimal,
    reduce_only: bool,
    post_only: bool = False,
) -> dict:
    # Back-compat shim: older code calls the private builder.
    return build_create_order_payload(
        acc=acc,
        instrument=instrument,
        size=size,
        inst_info=inst_info,
        is_buying=is_buying,
        is_market=is_market,
        time_in_force=time_in_force,
        tif_code=tif_code,
        price=price,
        reduce_only=reduce_only,
        post_only=post_only,
    )


def _create_order(acc: AccountConfig, cookie: str, payload: dict) -> dict | None:
    for _ in range(3):
        try:
            r = post("/lite/v1/create_order", acc=acc, cookie=cookie, payload=payload, timeout=30)
            if r.status_code != 503:
                return r.json()
        except Exception as e:
            debug("_create_order failed", exc=e)
            pass
        time.sleep(1)
    return None


def place_limit_order(
    acc: AccountConfig,
    cookie: str,
    instrument: str,
    inst_info: dict,
    size: Decimal,
    price: Decimal,
    is_buying: bool,
    reduce_only: bool = False,
    post_only: bool = False,
) -> dict | None:
    payload = _build_order_payload(
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
    )
    return _create_order(acc, cookie, payload)


def place_ioc_order(
    acc: AccountConfig,
    cookie: str,
    instrument: str,
    inst_info: dict,
    size: Decimal,
    price: Decimal,
    is_buying: bool,
    reduce_only: bool = False,
) -> dict | None:
    payload = _build_order_payload(
        acc=acc,
        instrument=instrument,
        size=size,
        inst_info=inst_info,
        is_buying=is_buying,
        is_market=False,
        time_in_force="IMMEDIATE_OR_CANCEL",
        tif_code=3,
        price=price,
        reduce_only=reduce_only,
    )
    return _create_order(acc, cookie, payload)


def place_market_order(
    acc: AccountConfig,
    cookie: str,
    instrument: str,
    inst_info: dict,
    size: Decimal,
    is_buying: bool,
    reduce_only: bool = False,
) -> dict | None:
    payload = _build_order_payload(
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
    )
    return _create_order(acc, cookie, payload)
