from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from grvt_volume_boost.config import AccountConfig
from grvt_volume_boost.settings import ORIGIN, TRADES_URL

# Module-level session with connection pooling for keep-alive
_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=Retry(total=2, backoff_factor=0.3))
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def make_headers(acc: AccountConfig, cookie: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "Referer": f"{ORIGIN}/",
        "X-Api-Source": "WEB",
        "X-Grvt-Account-Id": acc.main_account_id,
        "Cookie": f"gravity={cookie}",
    }


def post(path: str, *, acc: AccountConfig, cookie: str, payload: dict, timeout: float = 30) -> requests.Response:
    return _session.post(f"{TRADES_URL}{path}", json=payload, headers=make_headers(acc, cookie), timeout=timeout)
