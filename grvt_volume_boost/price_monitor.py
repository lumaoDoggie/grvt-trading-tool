"""Continuous WebSocket ticker monitor for price stability checks.

This module provides real-time price monitoring via WebSocket, enabling
non-blocking price stability checks using historical data instead of
waiting for new samples.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

import websockets

from urllib.parse import urlparse, urlunparse

from grvt_volume_boost.settings import MARKET_DATA_URL


def _market_data_ws_url() -> str:
    """Convert MARKET_DATA_URL (http/https) to the matching ws/wss endpoint."""
    parsed = urlparse(MARKET_DATA_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/ws/full", "", "", ""))


@dataclass
class PriceSample:
    """A single price observation from the ticker stream."""
    timestamp: float
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


class PriceBuffer:
    """Ring buffer storing recent ticker data for stability analysis.
    
    Maintains a sliding window of price samples for checking whether
    the mid-price has remained stable within bid/ask bounds.
    
    Can track our own maker order prices to exclude them from stability
    calculations, so only external market activity is considered.
    """

    def __init__(self, max_age_sec: float = 3.0):
        self._max_age = max_age_sec
        self._samples: deque[PriceSample] = deque()
        self._lock = threading.Lock()
        # Track our own order prices to exclude from stability checks
        self._our_order_prices: set[Decimal] = set()

    def add(self, bid: Decimal, ask: Decimal) -> None:
        """Add a new price sample."""
        sample = PriceSample(timestamp=time.time(), bid=bid, ask=ask)
        with self._lock:
            self._samples.append(sample)
            self._prune()

    def register_our_order(self, price: Decimal) -> None:
        """Register a price as one of our maker orders."""
        with self._lock:
            self._our_order_prices.add(price)

    def unregister_our_order(self, price: Decimal) -> None:
        """Remove a price from our maker orders."""
        with self._lock:
            self._our_order_prices.discard(price)

    def clear_our_orders(self) -> None:
        """Clear all tracked order prices."""
        with self._lock:
            self._our_order_prices.clear()

    def _prune(self) -> None:
        """Remove samples older than max_age."""
        cutoff = time.time() - self._max_age
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def _is_our_price(self, price: Decimal) -> bool:
        """Check if a price matches one of our orders (within small tolerance)."""
        for our_price in self._our_order_prices:
            # Small tolerance for floating point comparison
            if abs(price - our_price) < Decimal("0.0000001"):
                return True
        return False

    def is_stable(self, window_sec: float = 2.0) -> bool:
        """Check if mid-price stayed within bid/ask bounds over the window.
        
        Returns True if throughout the observation window, the mid-price
        stayed within the current bid/ask range. This indicates no external
        trades have pushed the price outside reasonable bounds.
        
        Excludes price changes at levels where we have maker orders.
        """
        with self._lock:
            self._prune()
            if not self._samples:
                return False

            cutoff = time.time() - window_sec
            # Need at least some samples within the window
            window_samples = [s for s in self._samples if s.timestamp >= cutoff]
            if len(window_samples) < 2:
                return False

            # Get current bounds (latest sample)
            current = window_samples[-1]
            current_bid, current_ask = current.bid, current.ask

            # Check: did the mid-price of earlier samples stay within current bounds?
            for sample in window_samples:
                mid = sample.mid
                # Skip if this price level matches our order
                if self._is_our_price(sample.bid) or self._is_our_price(sample.ask):
                    continue
                if mid < current_bid or mid > current_ask:
                    return False

            return True

    def has_sufficient_data(self, min_samples: int = 3, min_age_sec: float = 1.5) -> bool:
        """Check if buffer has enough data for stability analysis."""
        with self._lock:
            if len(self._samples) < min_samples:
                return False
            if not self._samples:
                return False
            oldest = self._samples[0].timestamp
            return (time.time() - oldest) >= min_age_sec

    def get_latest(self) -> PriceSample | None:
        """Get the most recent price sample."""
        with self._lock:
            return self._samples[-1] if self._samples else None

    def get_spread_ticks(self, tick_size: Decimal) -> int | None:
        """Calculate spread in number of ticks from latest sample."""
        latest = self.get_latest()
        if not latest or tick_size <= 0:
            return None
        spread = latest.ask - latest.bid
        return int(spread / tick_size)


class TickerMonitor:
    """Manages WebSocket subscription to v1.ticker.s stream for an instrument.
    
    Runs in a background thread, continuously receiving ticker updates
    and storing them in a PriceBuffer for stability analysis.
    """

    def __init__(self, instrument: str, *, on_error: Callable[[str], None] | None = None):
        self.instrument = instrument
        self._buffer = PriceBuffer(max_age_sec=3.0)
        self._on_error = on_error
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

    @property
    def buffer(self) -> PriceBuffer:
        return self._buffer

    def start(self) -> None:
        """Start the background ticker subscription thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the ticker subscription."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._connected = False

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_stable(self, window_sec: float = 2.0) -> bool:
        """Check price stability using buffered data."""
        return self._buffer.is_stable(window_sec)

    def has_data(self) -> bool:
        """Check if buffer has sufficient data."""
        return self._buffer.has_sufficient_data()

    def _run_loop(self) -> None:
        """Background thread entry point."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._subscribe_loop())
        except Exception as e:
            if self._on_error:
                self._on_error(f"[TickerMonitor] Fatal error: {e}")
        finally:
            loop.close()

    async def _subscribe_loop(self) -> None:
        """WebSocket subscribe loop with reconnection."""
        while not self._stop_event.is_set():
            try:
                await self._connect_and_listen()
            except Exception as e:
                if self._on_error:
                    self._on_error(f"[TickerMonitor] Connection error: {e}")
                self._connected = False
                # Wait before reconnecting
                for _ in range(10):
                    if self._stop_event.is_set():
                        return
                    await asyncio.sleep(0.5)

    async def _connect_and_listen(self) -> None:
        """Connect to WebSocket and process ticker updates."""
        # Market data WebSocket endpoint (public, no auth needed).
        # Use the env-selected MARKET_DATA_URL so TESTNET works correctly.
        ws_url = _market_data_ws_url()
        
        async with websockets.connect(ws_url, close_timeout=2) as ws:
            # Subscribe to ticker stream
            subscribe_msg = {
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {
                    "stream": "v1.ticker.s",
                    "selectors": [self.instrument],
                },
                "id": 1,
            }
            await ws.send(json.dumps(subscribe_msg))
            self._connected = True

            while not self._stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(msg)
                    self._process_ticker_message(data)
                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError:
                    continue

    def _process_ticker_message(self, data: dict) -> None:
        """Extract bid/ask from ticker message and add to buffer."""
        try:
            # Handle both feed format and direct result format
            result = data.get("params", {}).get("result", data.get("result", {}))
            if isinstance(result, dict):
                feed = result.get("feed", result)
                if isinstance(feed, dict):
                    bid_str = feed.get("best_bid_price") or feed.get("bid")
                    ask_str = feed.get("best_ask_price") or feed.get("ask")
                    if bid_str and ask_str:
                        self._buffer.add(Decimal(str(bid_str)), Decimal(str(ask_str)))
        except Exception:
            pass  # Ignore malformed messages


def get_spread_info(instrument: str, tick_size: Decimal) -> tuple[int | None, Decimal | None, Decimal | None]:
    """Get spread in ticks for an instrument using REST API.
    
    Returns (spread_ticks, bid, ask) or (None, None, None) on error.
    """
    from grvt_volume_boost.clients.market_data import get_ticker
    try:
        ticker = get_ticker(instrument)
        bid = Decimal(str(ticker.get("best_bid_price", "0")))
        ask = Decimal(str(ticker.get("best_ask_price", "0")))
        if bid <= 0 or ask <= 0 or tick_size <= 0:
            return None, None, None
        spread = ask - bid
        spread_ticks = int(spread / tick_size)
        return spread_ticks, bid, ask
    except Exception:
        return None, None, None
