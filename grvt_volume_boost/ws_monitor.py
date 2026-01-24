"""WebSocket-based position and order monitor for real-time updates."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Callable

import websockets

from grvt_volume_boost.services.orders import get_open_orders
from grvt_volume_boost.settings import WS_URL
from grvt_volume_boost.ws_compat import connect as ws_connect

if TYPE_CHECKING:
    from grvt_volume_boost.config import AccountConfig


@dataclass
class AccountState:
    """State for a single account's positions and orders."""
    positions: dict[str, Decimal] = field(default_factory=dict)  # instrument -> size
    open_orders: dict[str, list[dict]] = field(default_factory=dict)  # instrument -> [orders]
    connected: bool = False
    last_update: float = 0.0


class PositionWSManager:
    """Manages WebSocket connections for real-time position/order updates.
    
    Maintains persistent connections for two accounts and updates state
    based on v1.order stream events.
    """

    def __init__(
        self,
        acc1: "AccountConfig",
        acc2: "AccountConfig",
        cookie_getter1: Callable[[], str | None],
        cookie_getter2: Callable[[], str | None],
        on_update: Callable[[], None] | None = None,
    ):
        self.acc1 = acc1
        self.acc2 = acc2
        self.cookie_getter1 = cookie_getter1
        self.cookie_getter2 = cookie_getter2
        self.on_update = on_update

        self.state1 = AccountState()
        self.state2 = AccountState()

        # State is read by the GUI thread; protect it.
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Start WS connections in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop WS connections."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _run_loop(self) -> None:
        """Run the asyncio event loop for WS connections."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        """Main async task - runs both account listeners concurrently."""
        await asyncio.gather(
            self._listen_account(self.acc1, self.cookie_getter1, self.state1, "acc1"),
            self._listen_account(self.acc2, self.cookie_getter2, self.state2, "acc2"),
        )

    async def _listen_account(
        self,
        acc: "AccountConfig",
        cookie_getter: Callable[[], str | None],
        state: AccountState,
        label: str,
    ) -> None:
        """Listen to WS updates for a single account."""
        while not self._stop_event.is_set():
            ws = None
            try:
                cookie = cookie_getter()
                if not cookie:
                    state.connected = False
                    await asyncio.sleep(2.0)
                    continue

                # Subscribe to order updates for all instruments
                ws = await ws_connect(
                    WS_URL,
                    headers={
                        "Cookie": f"gravity={cookie}",
                        "X-Grvt-Account-Id": str(acc.main_account_id),
                    },
                    close_timeout=2,
                )

                # Subscribe to v1.order stream
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "subscribe",
                    "params": {"stream": "v1.order", "selectors": [str(acc.sub_account_id)]},
                    "id": 1,
                }))

                with self._state_lock:
                    state.connected = True
                    state.last_update = time.time()
                    # Seed with a REST snapshot once per (re)connect so the monitor reflects
                    # existing open orders even if they were placed before we subscribed.
                    state.open_orders.clear()
                    snapshot = get_open_orders(acc, cookie, instrument=None)
                    if snapshot:
                        for o in snapshot:
                            legs = o.get("legs") or o.get("l") or []
                            inst = None
                            if isinstance(legs, list) and legs:
                                inst = legs[0].get("instrument") or legs[0].get("i")
                            inst = inst or o.get("instrument") or o.get("i")
                            if not inst:
                                continue
                            state.open_orders.setdefault(str(inst), []).append(o)

                self._notify()

                while not self._stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        self._process_message(raw, state)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.ConnectionClosed:
                        break

            except Exception as e:
                print(f"[WS-{label}] Error: {e}")
                with self._state_lock:
                    state.connected = False
                self._notify()
                await asyncio.sleep(2.0)
            finally:
                with self._state_lock:
                    state.connected = False
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

    def _process_message(self, raw: str, state: AccountState) -> None:
        """Process a WS message and update state."""
        try:
            msg = json.loads(raw)
            order_data = msg.get("params", {}).get("result", msg.get("result", msg))
            if not isinstance(order_data, dict):
                return

            feed = order_data.get("feed", order_data)
            if not isinstance(feed, dict):
                return

            # Extract order info
            instrument = None
            legs = feed.get("legs") or feed.get("l", [])
            if legs and isinstance(legs, list) and len(legs) > 0:
                instrument = legs[0].get("instrument") or legs[0].get("i")

            if not instrument:
                return

            # Extract position from state if available
            state_info = feed.get("state", {})
            if isinstance(state_info, dict):
                status = state_info.get("status", "")
                
                # Try to get filled size from various fields
                filled_sizes = feed.get("s1", {}).get("bs", []) if isinstance(feed.get("s1"), dict) else []
                
                # Update order in our state
                order_id = feed.get("order_id") or feed.get("oid") or feed.get("id")
                
                with self._state_lock:
                    inst_key = str(instrument)
                    if status in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
                        # Remove from open orders
                        if inst_key in state.open_orders:
                            state.open_orders[inst_key] = [
                                o for o in state.open_orders[inst_key]
                                if str(o.get("order_id") or o.get("oid") or o.get("id")) != str(order_id)
                            ]
                            # Avoid leaving empty instrument keys around (prevents "ghost rows" in GUI).
                            if not state.open_orders[inst_key]:
                                del state.open_orders[inst_key]
                    elif status in ("OPEN", "PENDING"):
                        # Add/update in open orders
                        if inst_key not in state.open_orders:
                            state.open_orders[inst_key] = []
                        # Remove old version if exists
                        state.open_orders[inst_key] = [
                            o for o in state.open_orders[inst_key]
                            if str(o.get("order_id") or o.get("oid") or o.get("id")) != str(order_id)
                        ]
                        state.open_orders[inst_key].append(feed)

            with self._state_lock:
                state.last_update = time.time()
            self._notify()

        except Exception as e:
            print(f"[WS] Parse error: {e}")

    def _notify(self) -> None:
        """Notify listener of state change."""
        if self.on_update:
            try:
                self.on_update()
            except Exception:
                pass

    def get_all_positions(self) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        """Return (acc1_positions, acc2_positions)."""
        with self._state_lock:
            return dict(self.state1.positions), dict(self.state2.positions)

    def get_all_open_orders(self) -> tuple[dict[str, list], dict[str, list]]:
        """Return (acc1_orders, acc2_orders)."""
        with self._state_lock:
            # Filter out empty markets so the GUI doesn't render ghost rows.
            o1 = {k: list(v) for k, v in self.state1.open_orders.items() if v}
            o2 = {k: list(v) for k, v in self.state2.open_orders.items() if v}
            return o1, o2

    def is_connected(self) -> tuple[bool, bool]:
        """Return (acc1_connected, acc2_connected)."""
        with self._state_lock:
            return self.state1.connected, self.state2.connected
