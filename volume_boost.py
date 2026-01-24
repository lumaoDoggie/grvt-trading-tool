"""Volume Boost entry point (backwards-compatible).

The core implementation lives under `grvt_volume_boost/`.
"""

from grvt_volume_boost.auth.cookies import get_cookies_parallel, get_fresh_cookie
from grvt_volume_boost.clients.market_data import get_instrument, get_ticker
from grvt_volume_boost.services.orders import get_margin_ratio
from grvt_volume_boost.strategy import market_close, place_order_pair, place_order_pair_with_retry

from grvt_volume_boost.cli_volume_boost import main

__all__ = [
    "get_fresh_cookie",
    "get_cookies_parallel",
    "get_instrument",
    "get_ticker",
    "get_margin_ratio",
    "place_order_pair",
    "place_order_pair_with_retry",
    "market_close",
    "main",
]


if __name__ == "__main__":
    main()
