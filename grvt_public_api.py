"""Compatibility wrapper for the Volume Boost public market-data client."""

from grvt_volume_boost.clients.market_data import get_all_instruments, get_instrument, get_ticker, get_trades

__all__ = ["get_all_instruments", "get_instrument", "get_ticker", "get_trades"]
