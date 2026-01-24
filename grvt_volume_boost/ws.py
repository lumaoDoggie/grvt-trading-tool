from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Callable

import websockets

from grvt_volume_boost.settings import WS_URL


def _deep_contains(obj, needle: str) -> bool:
    """Best-effort recursive search for `needle` in dict/list/str payloads."""
    if obj is None:
        return False
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, (int, float, bool)):
        return False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and needle in k:
                return True
            if _deep_contains(v, needle):
                return True
        return False
    if isinstance(obj, list):
        return any(_deep_contains(v, needle) for v in obj)
    return False


def _extract_status_any(feed: dict, order_data: dict | None = None) -> str | None:
    """Extract a status string from a v1.order payload across known variants."""
    status = None

    state = feed.get("state")
    if isinstance(state, dict):
        status = state.get("status") or state.get("s")
    elif isinstance(state, str):
        status = state

    status = status or feed.get("status") or feed.get("s")

    s1 = feed.get("s1")
    if isinstance(s1, dict):
        status = status or s1.get("s") or s1.get("status")

    if not status and isinstance(order_data, dict):
        s1 = order_data.get("s1")
        if isinstance(s1, dict):
            status = s1.get("s") or s1.get("status")
        status = status or order_data.get("status") or order_data.get("s")

    return str(status).upper() if status else None


def _extract_instrument_any(feed: dict, order_data: dict | None = None) -> str | None:
    """Extract instrument from a v1.order payload across known variants."""
    legs = feed.get("legs") or feed.get("l") or []
    if isinstance(legs, list) and legs:
        leg0 = legs[0] if isinstance(legs[0], dict) else {}
        inst = leg0.get("instrument") or leg0.get("i")
        if inst:
            return str(inst)
    inst = feed.get("instrument") or feed.get("i")
    if inst:
        return str(inst)
    if isinstance(order_data, dict):
        legs = order_data.get("legs") or order_data.get("l") or []
        if isinstance(legs, list) and legs:
            leg0 = legs[0] if isinstance(legs[0], dict) else {}
            inst = leg0.get("instrument") or leg0.get("i")
            if inst:
                return str(inst)
        inst = order_data.get("instrument") or order_data.get("i")
        if inst:
            return str(inst)
    return None


def _extract_client_co_any(feed: dict, order_data: dict | None = None) -> str | None:
    """Extract client order id (co/nonce) across known variants."""
    co = None

    # Newer order feed payloads use metadata/signature objects (not "m"/"s").
    meta = feed.get("metadata") if isinstance(feed.get("metadata"), dict) else None
    if isinstance(meta, dict):
        co = meta.get("client_order_id") or meta.get("co")

    sig2 = feed.get("signature") if isinstance(feed.get("signature"), dict) else None
    if isinstance(sig2, dict) and co is None:
        co = sig2.get("nonce") or sig2.get("n")
    m = feed.get("m") if isinstance(feed.get("m"), dict) else None
    if isinstance(m, dict):
        co = m.get("co")
    co = co if co is not None else feed.get("co")

    # Signature object often contains the nonce under "n".
    sig = feed.get("s") if isinstance(feed.get("s"), dict) else None
    if isinstance(sig, dict) and co is None:
        co = sig.get("n")

    if co is None and isinstance(order_data, dict):
        meta = order_data.get("metadata") if isinstance(order_data.get("metadata"), dict) else None
        if isinstance(meta, dict):
            co = meta.get("client_order_id") or meta.get("co")

        sig2 = order_data.get("signature") if isinstance(order_data.get("signature"), dict) else None
        if isinstance(sig2, dict) and co is None:
            co = sig2.get("nonce") or sig2.get("n")

        m = order_data.get("m") if isinstance(order_data.get("m"), dict) else None
        if isinstance(m, dict):
            co = m.get("co")
        co = co if co is not None else order_data.get("co")
        sig = order_data.get("s") if isinstance(order_data.get("s"), dict) else None
        if isinstance(sig, dict) and co is None:
            co = sig.get("n")

    return str(co) if co is not None else None


def _extract_leg_fields_any(feed: dict, order_data: dict | None = None) -> tuple[str | None, str | None, bool | None]:
    """Extract (size, limit_price, is_buying) from legs across variants."""
    legs = feed.get("legs") or feed.get("l") or []
    if isinstance(legs, list) and legs:
        leg0 = legs[0] if isinstance(legs[0], dict) else {}
        size = leg0.get("s") or leg0.get("size")
        lp = leg0.get("lp") or leg0.get("limit_price")
        # Buy-side flag has multiple names depending on stream/endpoint.
        ib = (
            leg0.get("ib")
            if "ib" in leg0
            else leg0.get("is_buying_asset")
            if "is_buying_asset" in leg0
            else leg0.get("is_buying_contract")
        )
        if isinstance(ib, str):
            ib = True if ib.lower() == "true" else False if ib.lower() == "false" else None
        return (str(size) if size is not None else None, str(lp) if lp is not None else None, ib if isinstance(ib, bool) else None)
    if isinstance(order_data, dict):
        legs = order_data.get("legs") or order_data.get("l") or []
        if isinstance(legs, list) and legs:
            leg0 = legs[0] if isinstance(legs[0], dict) else {}
            size = leg0.get("s") or leg0.get("size")
            lp = leg0.get("lp") or leg0.get("limit_price")
            ib = (
                leg0.get("ib")
                if "ib" in leg0
                else leg0.get("is_buying_asset")
                if "is_buying_asset" in leg0
                else leg0.get("is_buying_contract")
            )
            if isinstance(ib, str):
                ib = True if ib.lower() == "true" else False if ib.lower() == "false" else None
            return (str(size) if size is not None else None, str(lp) if lp is not None else None, ib if isinstance(ib, bool) else None)
    return None, None, None


class OrderStreamClient:
    """Persistent authenticated WS subscriber for v1.order.

    Used by the trading strategy to confirm maker orders without paying the
    connect/subscribe cost on every order and without relying on order_id (often 0x00).
    """

    def __init__(
        self,
        *,
        cookie_getter: Callable[[], str | None],
        main_account_id: str,
        sub_account_id: str,
        instrument: str | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.cookie_getter = cookie_getter
        self.main_account_id = str(main_account_id)
        self.sub_account_id = str(sub_account_id)
        # Optional filter. We subscribe to all instruments (selector=sub_account_id) and
        # filter client-side because "<sub>-<instrument>" selectors have been unreliable.
        self.instrument = instrument
        self.on_error = on_error

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._waiters: dict[str, list[threading.Event]] = {}
        self._seen: dict[str, float] = {}  # co -> timestamp
        self._events: deque[dict] = deque(maxlen=400)  # last parsed events for fallback matching

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._connected = False

    def is_connected(self) -> bool:
        return bool(self._connected)

    def wait_for_client_co(self, client_co: str, *, timeout: float = 5.0) -> bool:
        want = str(client_co)
        # Fast-path: already observed recently.
        with self._lock:
            ts = self._seen.get(want)
            if ts and (time.time() - ts) < 30.0:
                return True
            ev = threading.Event()
            self._waiters.setdefault(want, []).append(ev)

        ok = ev.wait(timeout=timeout)
        # Cleanup waiter list.
        with self._lock:
            lst = self._waiters.get(want) or []
            if ev in lst:
                lst.remove(ev)
            if not lst and want in self._waiters:
                self._waiters.pop(want, None)
        return ok

    def wait_for_maker_confirm(
        self,
        *,
        client_co: str | None,
        instrument: str,
        size: str,
        price: str,
        is_buying: bool,
        timeout: float = 5.0,
    ) -> bool:
        """Confirm we received an order update for the maker order.

        Prefer matching by client_co (nonce). If WS payload omits client_co, fall back to matching
        by (instrument, size, price, is_buying) within the recent event buffer.
        """
        deadline = time.time() + timeout
        want_co = str(client_co) if client_co is not None else None
        want_inst = str(instrument).lower()
        want_size = str(size)
        want_price = str(price)
        want_ib = bool(is_buying)

        # First try exact client co matching (fastest / least ambiguous).
        if want_co is not None:
            if self.wait_for_client_co(want_co, timeout=timeout):
                return True

        with self._cv:
            while time.time() < deadline:
                # Scan recent events for a match.
                for ev in reversed(self._events):
                    if str(ev.get("status", "")).upper() not in ("OPEN", "PENDING"):
                        continue
                    inst = str(ev.get("instrument", "")).lower()
                    if inst != want_inst:
                        continue
                    if want_co is not None and ev.get("co") is not None and str(ev.get("co")) == want_co:
                        return True
                    # Fallback: match by leg fields.
                    if (
                        str(ev.get("size")) == want_size
                        and str(ev.get("price")) == want_price
                        and (ev.get("is_buying") is None or bool(ev.get("is_buying")) == want_ib)
                    ):
                        return True

                remaining = max(0.0, deadline - time.time())
                if remaining <= 0:
                    break
                self._cv.wait(timeout=min(0.5, remaining))

        return False

    def _emit_error(self, msg: str) -> None:
        if self.on_error:
            try:
                self.on_error(msg)
                return
            except Exception:
                pass
        print(msg)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main(loop))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self, loop: asyncio.AbstractEventLoop) -> None:
        while not self._stop_event.is_set():
            ws = None
            try:
                cookie = self.cookie_getter()
                if not cookie:
                    self._connected = False
                    await asyncio.sleep(0.5)
                    continue

                selectors = [self.sub_account_id]
                # Some environments only deliver instrument-scoped order feeds. Subscribe to both
                # selectors to be resilient.
                if self.instrument:
                    selectors.append(f"{self.sub_account_id}-{self.instrument}")

                ws = await websockets.connect(
                    WS_URL,
                    extra_headers={
                        "Cookie": f"gravity={cookie}",
                        "X-Grvt-Account-Id": self.main_account_id,
                    },
                    close_timeout=2,
                )
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "subscribe",
                            "params": {"stream": "v1.order", "selectors": selectors},
                            "id": 1,
                        }
                    )
                )

                self._connected = True

                while not self._stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    self._process_message(raw)

            except Exception as e:
                self._connected = False
                self._emit_error(f"[WS] OrderStreamClient error: {type(e).__name__}: {e}")
                await asyncio.sleep(0.5)
            finally:
                self._connected = False
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

    def _process_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        order_data = msg.get("params", {}).get("result", msg.get("result", msg))
        if not isinstance(order_data, dict):
            return
        feed = order_data.get("feed", order_data)
        if not isinstance(feed, dict):
            return

        inst = _extract_instrument_any(feed, order_data)
        if self.instrument and inst and str(inst).lower() != str(self.instrument).lower():
            return
        if inst is None and self.instrument:
            # Some payload variants omit instrument. If we're subscribed for a single market,
            # treat it as that market so downstream matching works.
            inst = self.instrument

        status = _extract_status_any(feed, order_data)
        if status not in ("OPEN", "PENDING"):
            return

        co = _extract_client_co_any(feed, order_data)
        size, lp, ib = _extract_leg_fields_any(feed, order_data)

        now = time.time()
        with self._cv:
            if co is not None:
                got = str(co)
                self._seen[got] = now
            # prune old seen
            for k, ts in list(self._seen.items()):
                if (now - ts) > 60.0:
                    self._seen.pop(k, None)
            if inst:
                self._events.append(
                    {
                        "ts": now,
                        "co": co,
                        "instrument": inst,
                        "status": status,
                        "size": size,
                        "price": lp,
                        "is_buying": ib,
                    }
                )
            # Wake any waiters.
            if co is not None:
                waiters = list(self._waiters.get(str(co)) or [])
                for ev in waiters:
                    try:
                        ev.set()
                    except Exception:
                        pass
            self._cv.notify_all()


async def wait_for_order_on_book(
    cookie: str,
    sub_account_id: str,
    order_id: str,
    *,
    main_account_id: str,
    instrument: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """Subscribe to order updates, wait for an 'OPEN'/'PENDING' status for a given order_id."""
    try:
        headers = {
            "Cookie": f"gravity={cookie}",
            # Required for account-specific trading streams.
            "X-Grvt-Account-Id": str(main_account_id),
        }

        selector = str(sub_account_id)
        if instrument:
            selector = f"{sub_account_id}-{instrument}"

        async with websockets.connect(
            WS_URL,
            extra_headers=headers,
            close_timeout=2,
        ) as ws:
            subscribe_msg = {
                "jsonrpc": "2.0",
                "method": "subscribe",
                # New-style streams use `v1.*` with string selectors like "<sub>-<instrument>".
                # Default to "all instruments" selector (sub account id only).
                "params": {"stream": "v1.order", "selectors": [selector]},
                "id": 1,
            }
            await ws.send(json.dumps(subscribe_msg))

            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    data = json.loads(msg)
                    order_data = data.get("params", {}).get("result", data.get("result", data))

                    if isinstance(order_data, dict):
                        feed = order_data.get("feed", order_data)
                        if not isinstance(feed, dict):
                            continue
                        oid = feed.get("order_id") or feed.get("oid")
                        state = feed.get("state", {}) if isinstance(feed.get("state"), dict) else {}
                        status = state.get("status") or feed.get("state") or feed.get("status")
                        if oid == order_id and status in ("OPEN", "PENDING"):
                            return True
                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"[WS] Connection error: {e}")

    return False


async def wait_for_order_on_book_by_client_co(
    cookie: str,
    sub_account_id: str,
    client_co: str,
    *,
    main_account_id: str,
    instrument: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """Wait for an 'OPEN'/'PENDING' order update containing client co (nonce).

    Useful when create_order responses return placeholder order ids (e.g. '0x00').
    """
    try:
        headers = {
            "Cookie": f"gravity={cookie}",
            "X-Grvt-Account-Id": str(main_account_id),
        }

        selectors = [str(sub_account_id)]
        if instrument:
            # Some environments only deliver instrument-scoped order feeds. Subscribe to both.
            selectors.append(f"{sub_account_id}-{instrument}")

        async with websockets.connect(
            WS_URL,
            extra_headers=headers,
            close_timeout=2,
        ) as ws:
            subscribe_msg = {
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {"stream": "v1.order", "selectors": selectors},
                "id": 1,
            }
            await ws.send(json.dumps(subscribe_msg))

            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    data = json.loads(msg)
                    order_data = data.get("params", {}).get("result", data.get("result", data))
                    if not isinstance(order_data, dict):
                        continue

                    feed = order_data.get("feed", order_data)
                    if not isinstance(feed, dict):
                        continue

                    if instrument:
                        inst = _extract_instrument_any(feed, order_data)
                        if inst and str(inst).lower() != str(instrument).lower():
                            continue

                    status = _extract_status_any(feed, order_data)
                    if status not in ("OPEN", "PENDING"):
                        continue

                    # Prefer exact match on known fields, then fall back to fuzzy contains.
                    want = str(client_co)
                    got = _extract_client_co_any(feed, order_data)

                    if (got is not None and str(got) == want) or _deep_contains(feed, want) or _deep_contains(order_data, want):
                        return True
                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"[WS] Connection error: {e}")

    return False


def wait_for_order_sync(
    cookie: str,
    sub_account_id: str,
    order_id: str,
    *,
    main_account_id: str,
    instrument: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """Synchronous wrapper for wait_for_order_on_book."""
    try:
        return asyncio.run(
            wait_for_order_on_book(
                cookie,
                sub_account_id,
                order_id,
                main_account_id=main_account_id,
                instrument=instrument,
                timeout=timeout,
            )
        )
    except Exception as e:
        print(f"[WS] Error: {e}")
        return False


def wait_for_order_by_client_co_sync(
    cookie: str,
    sub_account_id: str,
    client_co: str,
    *,
    main_account_id: str,
    instrument: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """Synchronous wrapper for wait_for_order_on_book_by_client_co()."""
    try:
        return asyncio.run(
            wait_for_order_on_book_by_client_co(
                cookie,
                sub_account_id,
                client_co,
                main_account_id=main_account_id,
                instrument=instrument,
                timeout=timeout,
            )
        )
    except Exception as e:
        print(f"[WS] Error: {e}")
        return False
