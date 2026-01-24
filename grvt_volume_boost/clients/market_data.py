from __future__ import annotations

import os
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from grvt_volume_boost.settings import MARKET_DATA_URL

# Module-level session with connection pooling for keep-alive
_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=Retry(total=2, backoff_factor=0.3))
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def _post(path: str, payload: dict) -> dict[str, Any]:
    r = _session.post(f"{MARKET_DATA_URL}{path}", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def _post_base(base: str, path: str, payload: dict) -> dict[str, Any]:
    r = _session.post(f"{base}{path}", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def _base_url(*, testnet: bool) -> str:
    if not testnet:
        return MARKET_DATA_URL
    return os.getenv("GRVT_MARKET_DATA_TESTNET_BASE_URL", "https://market-data.testnet.grvt.io")


def get_all_instruments(testnet: bool = False) -> list[dict]:
    base = _base_url(testnet=testnet)
    return _post_base(base, "/full/v1/all_instruments", {}).get("result", [])


def get_instrument(instrument: str, testnet: bool = False) -> dict:
    base = _base_url(testnet=testnet)
    return _post_base(base, "/full/v1/instrument", {"instrument": instrument}).get("result", {})


def get_ticker(instrument: str, testnet: bool = False) -> dict:
    base = _base_url(testnet=testnet)
    return _post_base(base, "/full/v1/ticker", {"instrument": instrument}).get("result", {})


def get_trades(instrument: str, limit: int = 20, testnet: bool = False) -> list[dict]:
    base = _base_url(testnet=testnet)
    return _post_base(base, "/full/v1/trade", {"instrument": instrument, "limit": limit}).get("result", [])

