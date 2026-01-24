from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


REPO_ROOT = Path(__file__).resolve().parent.parent

ENV = (os.getenv("GRVT_ENV", "prod") or "prod").strip().lower()
if ENV not in ("prod", "testnet"):
    ENV = "prod"


def _env_url(name: str, *, default_prod: str, default_testnet: str) -> str:
    raw = os.getenv(name)
    if raw is not None and raw != "":
        return raw
    return default_testnet if ENV == "testnet" else default_prod


_DEFAULT_CHAIN_ID = 326 if ENV == "testnet" else 325
# Many users keep `GRVT_CHAIN_ID=325` in .env (prod default). If they switch the GUI to
# TESTNET, that breaks EIP-712 verification. We auto-correct the common mismatch.
try:
    _raw_chain_id = os.getenv("GRVT_CHAIN_ID")
    if _raw_chain_id is None or _raw_chain_id == "":
        CHAIN_ID = _DEFAULT_CHAIN_ID
    else:
        _ci = int(_raw_chain_id)
        if ENV == "testnet" and _ci == 325:
            CHAIN_ID = 326
        elif ENV == "prod" and _ci == 326:
            CHAIN_ID = 325
        else:
            CHAIN_ID = _ci
except Exception:
    CHAIN_ID = _DEFAULT_CHAIN_ID
TRADES_URL = _env_url(
    "GRVT_TRADES_BASE_URL",
    default_prod="https://trades.grvt.io",
    default_testnet="https://trades.testnet.grvt.io",
)
MARKET_DATA_URL = _env_url(
    "GRVT_MARKET_DATA_BASE_URL",
    default_prod="https://market-data.grvt.io",
    default_testnet="https://market-data.testnet.grvt.io",
)
ORIGIN = _env_url(
    "GRVT_ORIGIN",
    default_prod="https://grvt.io",
    default_testnet="https://testnet.grvt.io",
)
WS_URL = _env_url(
    "GRVT_WS_URL",
    default_prod="wss://trades.grvt.io/ws/full",
    default_testnet="wss://trades.testnet.grvt.io/ws/full",
)
EDGE_URL = _env_url(
    "GRVT_EDGE_URL",
    default_prod="https://edge.grvt.io",
    default_testnet="https://edge.testnet.grvt.io",
)

SESSION_DIR = REPO_ROOT / ("session_testnet" if ENV == "testnet" else "session")
COOKIE_CACHE_FILE = REPO_ROOT / ("grvt_cookie_cache_testnet.json" if ENV == "testnet" else "grvt_cookie_cache.json")
