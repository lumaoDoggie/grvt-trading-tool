"""Compatibility wrapper for `grvt_volume_boost.services.orders`."""

from grvt_volume_boost.services.orders import (
    cancel_all_orders,
    cancel_order,
    get_margin_ratio,
    get_position_size,
    place_ioc_order,
    place_limit_order,
    place_market_order,
)
from grvt_volume_boost.services.signing import EIP712_ORDER_TYPE

__all__ = [
    "EIP712_ORDER_TYPE",
    "get_margin_ratio",
    "get_position_size",
    "cancel_order",
    "cancel_all_orders",
    "place_limit_order",
    "place_ioc_order",
    "place_market_order",
]
