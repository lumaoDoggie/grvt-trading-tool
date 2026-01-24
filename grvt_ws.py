"""Compatibility wrapper for `grvt_volume_boost.ws`."""

from grvt_volume_boost.ws import wait_for_order_on_book, wait_for_order_sync

__all__ = ["wait_for_order_on_book", "wait_for_order_sync"]
