from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


@dataclass(frozen=True)
class NormalizedSize:
    size: Decimal
    size_step: Decimal
    min_size: Decimal
    min_notional: Decimal


def _decimal_field(inst_info: dict, key: str, default: str = "0") -> Decimal:
    raw = inst_info.get(key, default)
    if raw is None:
        raw = default
    return Decimal(str(raw))


def _base_decimals(inst_info: dict) -> int:
    raw = inst_info.get("base_decimals", 9)
    try:
        return int(raw)
    except Exception:
        return 9


def _size_quantum(inst_info: dict) -> Decimal:
    base_decimals = _base_decimals(inst_info)
    return Decimal(1) / (Decimal(10) ** base_decimals)


def normalize_size(inst_info: dict, size: Decimal) -> NormalizedSize:
    """Round size DOWN to a safe step and validate against min constraints.

    This targets the common GRVT error: "Order size too granular".
    """
    quantum = _size_quantum(inst_info)
    min_size = _decimal_field(inst_info, "min_size", "0")
    min_notional = _decimal_field(inst_info, "min_notional", "0")

    # Conservative step: must satisfy base_decimals and min_size.
    size_step = max(quantum, min_size) if min_size > 0 else quantum

    # Round down to a multiple of size_step.
    steps = (size / size_step).to_integral_value(rounding=ROUND_DOWN)
    normalized = steps * size_step
    normalized = normalized.quantize(quantum, rounding=ROUND_DOWN)

    if normalized <= 0:
        raise ValueError("Computed size is 0 after rounding")
    if min_size and normalized < min_size:
        raise ValueError(f"Size {normalized} is below min_size {min_size}")

    return NormalizedSize(
        size=normalized,
        size_step=size_step,
        min_size=min_size,
        min_notional=min_notional,
    )


def mid_price_from_ticker(ticker: dict) -> Decimal:
    bid = Decimal(str(ticker.get("best_bid_price") or "0"))
    ask = Decimal(str(ticker.get("best_ask_price") or "0"))
    if bid <= 0 or ask <= 0:
        raise ValueError(f"Invalid best bid/ask in ticker: bid={bid}, ask={ask}")
    return (bid + ask) / 2


def compute_size_from_usd_notional(inst_info: dict, ticker: dict, notional_usd: Decimal) -> tuple[Decimal, Decimal]:
    """Return (size, mid_price).
    
    Rounds UP to ensure the computed size * mid >= notional_usd and min_notional.
    """
    if notional_usd <= 0:
        raise ValueError("Notional must be > 0")

    mid = mid_price_from_ticker(ticker)
    raw_size = notional_usd / mid
    
    # Get sizing parameters
    quantum = _size_quantum(inst_info)
    min_size = _decimal_field(inst_info, "min_size", "0")
    min_notional = _decimal_field(inst_info, "min_notional", "0")
    size_step = max(quantum, min_size) if min_size > 0 else quantum
    
    # Round UP to ensure we meet the requested notional (and min_notional)
    from decimal import ROUND_UP
    steps = (raw_size / size_step).to_integral_value(rounding=ROUND_UP)
    size = steps * size_step
    size = size.quantize(quantum, rounding=ROUND_UP)
    
    # Ensure at least min_size
    if min_size and size < min_size:
        size = min_size

    if size <= 0:
        raise ValueError("Computed size is 0 after rounding")
    
    # Final check: if still below min_notional, bump up one step
    if min_notional and (size * mid) < min_notional:
        extra_steps = ((min_notional / mid - size) / size_step).to_integral_value(rounding=ROUND_UP)
        size = size + extra_steps * size_step
        size = size.quantize(quantum, rounding=ROUND_UP)

    return size, mid

